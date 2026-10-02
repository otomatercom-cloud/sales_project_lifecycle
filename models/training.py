from odoo import _, api, fields, models
from odoo.exceptions import AccessError, UserError, ValidationError

ADMIN = 'sales_project_lifecycle.group_lifecycle_admin'
ACTIVE = ('scheduled', 'in_progress')
TRAINING_MATRIX = {
    'start': {'from': ('scheduled',), 'to': 'in_progress'},
    'complete': {'from': ('in_progress',), 'to': 'completed'},
    'cancel': {'from': ('scheduled', 'in_progress'), 'to': 'cancelled'},
}
CONTENT_FIELDS = {'participants', 'topics', 'notes', 'materials', 'materials_filename',
                  'recording', 'recording_filename', 'customer_confirmation',
                  'message_main_attachment_id'}


class OtmTraining(models.Model):
    _name = 'otm.training'
    _description = 'Customer Training Session'
    _inherit = ['mail.thread', 'otm.transition.mixin']
    _order = 'date desc, start_time'
    _otm_state_field = 'status'
    _otm_matrix = TRAINING_MATRIX

    def _otm_email_partner(self):
        return self.project_id.sudo().partner_id
    _otm_reason_methods = ('action_cancel',)

    name = fields.Char(string='Reference', readonly=True, copy=False, default=lambda s: _('New'))
    project_id = fields.Many2one('project.project', required=True, index=True, ondelete='restrict')
    customer_id = fields.Many2one('res.partner', related='project_id.partner_id', store=True)
    sales_team_id = fields.Many2one('otm.sales.team', related='project_id.otm_sales_team_id', store=True)
    sales_head_id = fields.Many2one('res.users', related='project_id.otm_sales_head_id', store=True)
    required = fields.Boolean(
        default=True, help="Required sessions must all be completed before the 30% payment falls due.")
    training_type = fields.Selection(
        [('online', 'Online'), ('onsite', 'On-site'), ('video', 'Video')], required=True, default='online')
    trainer_id = fields.Many2one(
        'res.users', string='Trainer', required=True, index=True, tracking=True,
        default=lambda self: self.env['project.project'].browse(
            self.env.context.get('default_project_id')).sudo().otm_trainer_id)
    date = fields.Date(required=True, tracking=True)
    start_time = fields.Float(default=10.0, required=True)
    end_time = fields.Float(default=11.0, required=True)
    participants = fields.Text(help="Customer attendees")
    topics = fields.Text()
    notes = fields.Text()
    materials = fields.Binary(attachment=True)
    materials_filename = fields.Char()
    recording = fields.Binary(attachment=True)
    recording_filename = fields.Char()
    status = fields.Selection([
        ('scheduled', 'Scheduled'), ('in_progress', 'In Progress'),
        ('completed', 'Completed'), ('cancelled', 'Cancelled'),
    ], default='scheduled', required=True, tracking=True, index=True, copy=False)
    customer_confirmation = fields.Boolean(string='Customer Confirmed')
    completed_date = fields.Datetime(readonly=True, copy=False)
    cancel_reason = fields.Text(readonly=True, copy=False)

    @api.constrains('start_time', 'end_time')
    def _check_times(self):
        for t in self:
            if not (0.0 <= t.start_time < 24.0 and 0.0 < t.end_time <= 24.0) or t.end_time <= t.start_time:
                raise ValidationError(_("The training end time must be after the start time (same day)."))

    @api.constrains('trainer_id', 'date', 'start_time', 'end_time', 'status')
    def _check_trainer_free(self):
        for t in self.filtered(lambda t: t.status in ACTIVE):
            clash = self.sudo().search([
                ('id', '!=', t.id), ('trainer_id', '=', t.trainer_id.id), ('date', '=', t.date),
                ('status', 'in', ACTIVE), ('start_time', '<', t.end_time), ('end_time', '>', t.start_time)],
                limit=1)
            if clash:
                raise ValidationError(_(
                    "%(who)s already has a training session on %(date)s during this time.",
                    who=t.trainer_id.name, date=t.date))

    @api.model_create_multi
    def create(self, vals_list):
        seq = self.env['ir.sequence'].sudo()
        for vals in vals_list:
            vals['name'] = seq.next_by_code('otm.training') or _('New')
            if not self.env.su:
                project = self.env['project.project'].sudo().browse(vals.get('project_id'))
                if not (self.env.user.has_group(ADMIN) or self.env.user == project.user_id):
                    raise AccessError(_("Only the Project Head can schedule training."))
                if project.otm_state != 'in_progress':
                    raise UserError(_("The project is not in progress."))
                for f in ('status', 'completed_date', 'cancel_reason'):
                    vals.pop(f, None)
                trainer = self.env['res.users'].browse(vals.get('trainer_id'))
                if trainer and project.otm_trainer_id and trainer != project.otm_trainer_id:
                    raise UserError(_("The trainer must be the project's trainer (%s).", project.otm_trainer_id.name))
        sessions = super().create(vals_list)
        sessions._otm_send_mail('sales_project_lifecycle.mail_training_confirmation')
        return sessions

    def write(self, vals):
        if not (self.env.su or self.env.context.get('otm_transition')):
            if {'status', 'completed_date', 'cancel_reason', 'project_id'} & vals.keys():
                raise UserError(_("The training status cannot be edited directly. Use the workflow buttons."))
            for t in self:
                if t.status in ('completed', 'cancelled'):
                    raise UserError(_("A finished training session cannot be changed."))
                if not t._otm_is_head_or_admin():
                    if self.env.user != t.trainer_id:
                        raise AccessError(_("Only the trainer or the Project Head can update this session."))
                    if set(vals) - CONTENT_FIELDS:
                        raise AccessError(_("The trainer can only update the session content."))
        return super().write(vals)

    def unlink(self):
        if not self.env.su:
            raise UserError(_("Training sessions cannot be deleted. Cancel them instead."))
        return super().unlink()

    def _otm_is_head_or_admin(self):
        return self.env.su or self.env.user.has_group(ADMIN) or self.env.user == self.sudo().project_id.user_id

    def _otm_log_scope(self):
        return (self.sales_team_id, self.sales_head_id)

    def _otm_check_actor(self, action):
        self.ensure_one()
        if self._otm_is_head_or_admin() or self.env.user == self.trainer_id:
            return
        raise AccessError(_("Only the trainer or the Project Head can do this."))

    def _otm_prerequisites(self, action):
        self.ensure_one()
        missing = []
        p = self.project_id.sudo()
        if action in ('start', 'complete') and p.otm_state != 'in_progress':
            missing.append(_("The project is not in progress."))
        if action == 'start' and not p.otm_deployment_ids.filtered(
                lambda d: d.status in ('deployed', 'verification', 'completed')):
            missing.append(_("The system must be deployed before training starts."))
        if action == 'complete':
            if not (self.topics or '').strip():
                missing.append(_("Record the topics covered."))
            if not (self.participants or '').strip():
                missing.append(_("Record the participants."))
            if not self.customer_confirmation:
                missing.append(_("The customer has not confirmed the training."))
        return missing

    def _otm_after_transition(self, action, old_state, reason):
        for t in self:
            if action == 'complete':
                t.with_context(otm_transition=True).write({'completed_date': fields.Datetime.now()})
            elif action == 'cancel':
                t.with_context(otm_transition=True).write({'cancel_reason': reason})
            if action in ('complete', 'cancel'):
                t.project_id.sudo()._otm_check_training_done()

    def _otm_require_reason(self, reason):
        if not (reason or '').strip():
            raise UserError(_("A reason is required for this action."))

    def action_start(self):
        return self._otm_do_transition('start')

    def action_complete(self):
        return self._otm_do_transition('complete')

    def action_cancel(self, reason=None):
        self._otm_require_reason(reason)
        return self._otm_do_transition('cancel', reason=reason)
