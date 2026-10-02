from odoo import _, api, fields, models
from odoo.exceptions import AccessError, UserError, ValidationError

DEMO_MATRIX = {
    'confirm': {'from': ('scheduled',), 'to': 'confirmed'},
    'complete': {'from': ('confirmed',), 'to': 'completed'},
    'cancel': {'from': ('scheduled', 'confirmed'), 'to': 'cancelled'},
    'no_show': {'from': ('scheduled', 'confirmed'), 'to': 'no_show'},
}
ACTIVE_STATUSES = ('scheduled', 'confirmed')


class OtmDemo(models.Model):
    _name = 'otm.demo'
    _description = 'Product Demo'
    _inherit = ['mail.thread', 'mail.activity.mixin', 'otm.transition.mixin']
    _order = 'demo_date desc, start_time'
    _otm_state_field = 'status'
    _otm_matrix = DEMO_MATRIX
    _otm_email_map = {'confirm': 'sales_project_lifecycle.mail_demo_confirmation'}

    def _otm_email_partner(self):
        return self.lead_id.customer_id
    _otm_reason_methods = ('action_cancel', 'action_no_show')

    name = fields.Char(string='Reference', readonly=True, copy=False, default=lambda s: _('New'))
    lead_id = fields.Many2one(
        'otm.lead', string='Lead', required=True, index=True, ondelete='restrict', tracking=True)
    sales_team_id = fields.Many2one(
        'otm.sales.team', related='lead_id.sales_team_id', store=True, index=True)
    salesperson_id = fields.Many2one(
        'res.users', related='lead_id.salesperson_id', store=True, index=True, string='Salesperson')
    demo_date = fields.Date(
        string='Demo Date', required=True, tracking=True, default=fields.Date.context_today)
    start_time = fields.Float(string='Start Time', required=True, default=10.0)
    end_time = fields.Float(string='End Time', required=True, default=11.0)
    demo_person_id = fields.Many2one(
        'res.users', string='Demo Person', required=True, tracking=True, index=True,
        default=lambda self: self.env.user, domain=[('share', '=', False)])
    status = fields.Selection([
        ('scheduled', 'Scheduled'),
        ('confirmed', 'Confirmed'),
        ('completed', 'Completed'),
        ('cancelled', 'Cancelled'),
        ('no_show', 'No Show'),
    ], default='scheduled', required=True, tracking=True, index=True, copy=False)
    notes = fields.Text()
    customer_confirmation = fields.Boolean(string='Customer Confirmed', tracking=True)
    outcome_reason = fields.Text(string='Cancel / No-show Reason', readonly=True, copy=False)
    completed_date = fields.Datetime(readonly=True, copy=False)
    completed_by_id = fields.Many2one('res.users', readonly=True, copy=False)

    # ------------------------------------------------------------------
    # Validation
    # ------------------------------------------------------------------
    @api.constrains('start_time', 'end_time')
    def _check_times(self):
        for demo in self:
            if not (0.0 <= demo.start_time < 24.0 and 0.0 < demo.end_time <= 24.0):
                raise ValidationError(_("Demo times must be between 00:00 and 24:00."))
            if demo.end_time <= demo.start_time:
                raise ValidationError(_("The demo end time must be after the start time."))

    @api.constrains('demo_person_id', 'demo_date', 'start_time', 'end_time', 'status')
    def _check_no_double_booking(self):
        for demo in self.filtered(lambda d: d.status in ACTIVE_STATUSES):
            # sudo: the clash may be with a demo of another team that this user cannot see
            clash = self.sudo().search([
                ('id', '!=', demo.id),
                ('demo_person_id', '=', demo.demo_person_id.id),
                ('demo_date', '=', demo.demo_date),
                ('status', 'in', ACTIVE_STATUSES),
                ('start_time', '<', demo.end_time),
                ('end_time', '>', demo.start_time),
            ], limit=1)
            if clash:
                raise ValidationError(_(
                    "%(person)s is already booked for another demo on %(date)s during this time slot.",
                    person=demo.demo_person_id.name, date=demo.demo_date))

    def _check_not_in_past(self):
        today = fields.Date.context_today(self)
        for demo in self:
            if demo.status in ACTIVE_STATUSES and demo.demo_date < today:
                raise ValidationError(_("A demo cannot be scheduled in the past."))

    # ------------------------------------------------------------------
    # Transition engine hooks
    # ------------------------------------------------------------------
    def _otm_log_scope(self):
        return (self.sales_team_id, self.salesperson_id)

    def _otm_check_actor(self, action):
        self.ensure_one()
        user = self.env.user
        if self.env.su or user.has_group('sales_project_lifecycle.group_lifecycle_admin'):
            return
        allowed = self.salesperson_id | self.lead_id.sudo().sales_head_id | self.demo_person_id
        if user not in allowed:
            raise AccessError(_(
                "Only the salesperson, the Sales Head of the team, or the demo person can do this."))

    def _otm_prerequisites(self, action):
        missing = []
        if action in ('confirm', 'complete') and not self.demo_person_id:
            missing.append(_("A demo person must be assigned."))
        if action == 'confirm' and self.demo_date < fields.Date.context_today(self):
            missing.append(_("The demo date is in the past."))
        if action == 'complete' and not (self.notes or '').strip():
            missing.append(_("Demo notes must be recorded before completing the demo."))
        return missing

    # ------------------------------------------------------------------
    # ORM
    # ------------------------------------------------------------------
    @api.model_create_multi
    def create(self, vals_list):
        sequence = self.env['ir.sequence'].sudo()
        for vals in vals_list:
            if vals.get('status', 'scheduled') != 'scheduled' and not self.env.su \
                    and not self.env.context.get('otm_transition'):
                raise UserError(_("A new demo must start as 'Scheduled'."))
            vals['name'] = sequence.next_by_code('otm.demo') or _('New')
        demos = super().create(vals_list)
        demos._check_not_in_past()
        return demos

    def write(self, vals):
        if 'status' in vals and not self.env.su and not self.env.context.get('otm_transition'):
            raise UserError(_("The demo status cannot be edited directly. Use the workflow buttons."))
        res = super().write(vals)
        if {'demo_date', 'start_time', 'end_time'} & vals.keys():
            self._check_not_in_past()
        return res

    # ------------------------------------------------------------------
    # Public business actions
    # ------------------------------------------------------------------
    def action_confirm(self):
        return self._otm_do_transition('confirm')

    def action_complete(self):
        return self._otm_do_transition('complete', extra_vals={
            'completed_date': fields.Datetime.now(), 'completed_by_id': self.env.user.id})

    def _otm_require_reason(self, reason):
        if not (reason or '').strip():
            raise UserError(_("A reason is required for this action."))

    def action_cancel(self, reason=None):
        self._otm_require_reason(reason)
        return self._otm_do_transition('cancel', reason=reason, extra_vals={'outcome_reason': reason})

    def action_no_show(self, reason=None):
        self._otm_require_reason(reason)
        return self._otm_do_transition('no_show', reason=reason, extra_vals={'outcome_reason': reason})
