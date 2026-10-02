from odoo import _, api, fields, models
from odoo.exceptions import AccessError, UserError, ValidationError

ADMIN = 'sales_project_lifecycle.group_lifecycle_admin'
PH = 'sales_project_lifecycle.group_project_head'

INTEGRATION_MATRIX = {
    'system_expiring': {'from': ('active',), 'to': 'expiring', 'system': True},
    'system_expire': {'from': ('active', 'expiring'), 'to': 'expired', 'system': True},
    'renew': {'from': ('active', 'expiring', 'expired'), 'to': 'active'},
    'cancel': {'from': ('active', 'expiring', 'expired'), 'to': 'cancelled'},
}


class OtmClientIntegration(models.Model):
    _name = 'otm.client.integration'
    _description = 'Client Integration'
    _inherit = ['mail.thread', 'mail.activity.mixin', 'otm.transition.mixin', 'otm.expiry.mixin']
    _order = 'expiry_date, id desc'
    _otm_state_field = 'status'
    _otm_matrix = INTEGRATION_MATRIX
    _otm_reason_methods = ('action_cancel',)

    name = fields.Char(string='Integration Name', required=True, tracking=True)
    customer_id = fields.Many2one('res.partner', required=True, index=True, tracking=True)
    project_id = fields.Many2one('project.project', index=True, ondelete='set null')
    integration_type = fields.Selection([
        ('whatsapp', 'WhatsApp'), ('payment', 'Payment gateway'), ('sms', 'SMS'), ('email', 'Email'),
        ('api', 'API'), ('mobile_app', 'Mobile App'), ('website', 'Website'), ('erp', 'ERP integration'),
        ('other', 'Other')], default='other', required=True)
    provider = fields.Char()
    description = fields.Text()
    company_id = fields.Many2one('res.company', default=lambda s: s.env.company)
    currency_id = fields.Many2one('res.currency', related='company_id.currency_id')
    setup_amount = fields.Monetary(currency_field='currency_id', tracking=True)
    recurring_amount = fields.Monetary(currency_field='currency_id', tracking=True)
    start_date = fields.Date()
    expiry_date = fields.Date(tracking=True)
    renewal_date = fields.Date(string='Next Renewal Date', tracking=True,
                               help="The date the integration is renewed to. Used by the Renew action.")
    responsible_developer_id = fields.Many2one('res.users', string='Responsible Developer', index=True,
                                               tracking=True, domain=[('share', '=', False)])
    sales_team_id = fields.Many2one('otm.sales.team', index=True)
    status = fields.Selection([
        ('active', 'Active'), ('expiring', 'Expiring'), ('expired', 'Expired'), ('cancelled', 'Cancelled')],
        default='active', required=True, tracking=True, index=True, copy=False)
    documentation = fields.Binary(attachment=True)
    documentation_filename = fields.Char()
    notes = fields.Text()
    cancel_reason = fields.Text(readonly=True, copy=False)

    @api.constrains('start_date', 'expiry_date', 'renewal_date', 'setup_amount', 'recurring_amount')
    def _check_values(self):
        for i in self:
            if i.setup_amount < 0 or i.recurring_amount < 0:
                raise ValidationError(_("Amounts cannot be negative."))
            if i.start_date and i.expiry_date and i.expiry_date < i.start_date:
                raise ValidationError(_("The expiry date cannot be before the start date."))

    def _otm_can_manage(self):
        u = self.env.user
        return self.env.su or u.has_group(ADMIN) or u.has_group(PH) or any(
            t.head_id == u for t in self.mapped('sales_team_id').sudo())

    @api.model_create_multi
    def create(self, vals_list):
        if not self.env.su:
            if not (self.env.user.has_group(ADMIN) or self.env.user.has_group(PH)
                    or self.env.user.otm_headed_team_ids):
                raise AccessError(_("Only a Sales Head, a Project Head or an Administrator can create an integration."))
            for v in vals_list:
                if v.get('status', 'active') != 'active' or v.get('cancel_reason'):
                    raise UserError(_("The status is managed by the workflow."))
        return super().create(vals_list)

    def write(self, vals):
        if not (self.env.su or self.env.context.get('otm_transition')):
            if {'status', 'cancel_reason'} & vals.keys():
                raise UserError(_("The status can only change through the workflow."))
            for i in self:
                if i.status == 'cancelled':
                    raise UserError(_("A cancelled integration cannot be edited."))
                if not (i._otm_can_manage() or self.env.user == i.responsible_developer_id):
                    raise AccessError(_("Only the responsible developer, the Sales Head or a Project Head can edit this."))
        return super().write(vals)

    def unlink(self):
        if not self.env.su:
            raise UserError(_("Integrations cannot be deleted. Cancel them instead."))
        return super().unlink()

    def _otm_log_scope(self):
        return (self.sales_team_id, self.responsible_developer_id)

    def _otm_check_actor(self, action):
        self.ensure_one()
        if not self._otm_can_manage():
            raise AccessError(_("Only the Sales Head, a Project Head or an Administrator can do this."))

    def _otm_prerequisites(self, action):
        self.ensure_one()
        missing = []
        if action == 'renew':
            if not self.renewal_date:
                missing.append(_("Set the next renewal date first."))
            elif self.expiry_date and self.renewal_date <= self.expiry_date:
                missing.append(_("The renewal date must be after the current expiry date."))
            elif self.renewal_date <= fields.Date.context_today(self):
                missing.append(_("The renewal date must be in the future."))
        return missing

    def _otm_after_transition(self, action, old_state, reason):
        for i in self:
            if action == 'renew':
                old = i.expiry_date
                i.with_context(otm_transition=True).write({'expiry_date': i.renewal_date, 'renewal_date': False})
                i.message_post(body=_("Renewed: expiry %(old)s → %(new)s.", old=old or '-', new=i.expiry_date))
            elif action == 'cancel':
                i.with_context(otm_transition=True).write({'cancel_reason': reason})

    def action_renew(self):
        return self._otm_do_transition('renew')

    def action_cancel(self, reason=None):
        if not (reason or '').strip():
            raise UserError(_("A reason is required for this action."))
        return self._otm_do_transition('cancel', reason=reason)

    def _otm_expiry_specs(self):
        return [('expiry_date', _("Integration expiry"))]

    def _otm_expiry_domain(self):
        return [('status', 'in', ('active', 'expiring', 'expired'))]

    def _otm_reminder_user(self):
        self.ensure_one()
        return self.responsible_developer_id or self.sales_team_id.head_id

    @api.model
    def _otm_after_expiry_sync(self, today, offsets):
        window = max(offsets)
        for i in self.search([('expiry_date', '!=', False), ('status', 'in', ('active', 'expiring'))]):
            days = (i.expiry_date - today).days
            if days < 0:
                i._otm_do_transition('system_expire')
            elif days <= window and i.status == 'active':
                i._otm_do_transition('system_expiring')
