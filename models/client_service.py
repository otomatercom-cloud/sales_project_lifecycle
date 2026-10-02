from odoo import _, api, fields, models
from odoo.exceptions import AccessError, UserError, ValidationError

from .expiry import PERIOD_MONTHS, add_period

ADMIN = 'sales_project_lifecycle.group_lifecycle_admin'
HEAD = 'sales_project_lifecycle.group_sales_head'
PH = 'sales_project_lifecycle.group_project_head'
FIN = 'sales_project_lifecycle.group_finance'
FIN_MGR = 'sales_project_lifecycle.group_finance_manager'

BILLING = [('one_time', 'One time'), ('monthly', 'Monthly'), ('quarterly', 'Quarterly'),
           ('half_yearly', 'Half yearly'), ('yearly', 'Yearly')]
RENEWAL_PERIOD = [('monthly', 'Monthly'), ('quarterly', 'Quarterly'),
                  ('half_yearly', 'Half yearly'), ('yearly', 'Yearly')]
KINDS = [('solution', 'Solution / Software'), ('hosting', 'Hosting'), ('domain', 'Domain'), ('ssl', 'SSL'),
         ('backup', 'Backup'), ('server_mgmt', 'Server Management'), ('amc', 'AMC'), ('support', 'Support'),
         ('other', 'Other')]

SERVICE_MATRIX = {
    'system_expiring': {'from': ('active', 'renewed'), 'to': 'expiring', 'system': True},
    'system_expire': {'from': ('active', 'renewed', 'expiring'), 'to': 'expired', 'system': True},
    'system_renew': {'from': ('active', 'renewed', 'expiring', 'expired'), 'to': 'renewed', 'system': True},
    'system_reactivate': {'from': ('expiring', 'expired', 'renewed'), 'to': 'active', 'system': True},
    'cancel': {'from': ('active', 'renewed', 'expiring', 'expired'), 'to': 'cancelled'},
}
RENEWAL_MATRIX = {
    'send_estimate': {'from': ('follow_up',), 'to': 'estimate_sent'},
    'approve': {'from': ('estimate_sent',), 'to': 'approved'},
    'confirm_payment': {'from': ('approved',), 'to': 'paid'},
    'renew': {'from': ('paid',), 'to': 'completed'},
    'cancel': {'from': ('follow_up', 'estimate_sent', 'approved'), 'to': 'cancelled'},
}


class OtmClientService(models.Model):
    _name = 'otm.client.service'
    _description = 'Client Service'
    _inherit = ['mail.thread', 'mail.activity.mixin', 'otm.transition.mixin', 'otm.expiry.mixin']
    _order = 'expiry_date, id desc'
    _otm_state_field = 'status'
    _otm_matrix = SERVICE_MATRIX
    _otm_reason_methods = ('action_cancel',)

    name = fields.Char(readonly=True, copy=False, default=lambda s: _('New'))
    customer_id = fields.Many2one('res.partner', required=True, index=True, tracking=True)
    project_id = fields.Many2one('project.project', index=True, ondelete='set null')
    deal_id = fields.Many2one('otm.deal', index=True, ondelete='set null', readonly=True, copy=False)
    service_id = fields.Many2one('otm.service')
    service_name = fields.Char(required=True, tracking=True)
    service_kind = fields.Selection(KINDS, default='solution', required=True)
    server_id = fields.Many2one('otm.client.server', string='Server')
    company_id = fields.Many2one('res.company', default=lambda s: s.env.company)
    currency_id = fields.Many2one('res.currency', related='company_id.currency_id')
    amount = fields.Monetary(currency_field='currency_id', tracking=True)
    billing_type = fields.Selection(BILLING, default='one_time', required=True, tracking=True)
    start_date = fields.Date(tracking=True)
    expiry_date = fields.Date(tracking=True)
    renewal_period = fields.Selection(RENEWAL_PERIOD)
    renewal_amount = fields.Monetary(currency_field='currency_id', tracking=True)
    responsible_user_id = fields.Many2one('res.users', string='Responsible', index=True, tracking=True,
                                          domain=[('share', '=', False)])
    sales_team_id = fields.Many2one('otm.sales.team', index=True, tracking=True)
    sales_head_id = fields.Many2one('res.users', related='sales_team_id.head_id', store=True, index=True)
    status = fields.Selection([
        ('active', 'Active'), ('expiring', 'Expiring'), ('expired', 'Expired'),
        ('renewed', 'Renewed'), ('cancelled', 'Cancelled')],
        default='active', required=True, tracking=True, index=True, copy=False)
    days_to_expiry = fields.Integer(compute='_compute_days_to_expiry')
    renewal_ids = fields.One2many('otm.service.renewal', 'service_id')
    renewal_count = fields.Integer(compute='_compute_renewal_count')
    last_renewed_date = fields.Date(readonly=True, copy=False)
    cancel_reason = fields.Text(readonly=True, copy=False)
    notes = fields.Text()

    PROTECTED = {'status', 'last_renewed_date', 'cancel_reason', 'deal_id', 'name'}

    @api.depends('expiry_date')
    def _compute_days_to_expiry(self):
        today = fields.Date.context_today(self)
        for s in self:
            s.days_to_expiry = (s.expiry_date - today).days if s.expiry_date else 0

    def _compute_renewal_count(self):
        for s in self:
            s.renewal_count = len(s.renewal_ids)

    @api.constrains('start_date', 'expiry_date', 'amount', 'renewal_amount', 'billing_type', 'renewal_period', 'status')
    def _check_values(self):
        for s in self:
            if s.start_date and s.expiry_date and s.expiry_date < s.start_date:
                raise ValidationError(_("The expiry date cannot be before the start date."))
            if s.amount < 0 or s.renewal_amount < 0:
                raise ValidationError(_("Amounts cannot be negative."))
            if s.billing_type != 'one_time' and s.status != 'cancelled':
                if not s.expiry_date:
                    raise ValidationError(_("A recurring service needs an expiry date."))
                if not s.renewal_period:
                    raise ValidationError(_("A recurring service needs a renewal period."))

    @api.onchange('service_id')
    def _onchange_service_id(self):
        if self.service_id and not self.service_name:
            self.service_name = self.service_id.name

    # -- access ---------------------------------------------------------
    def _otm_can_manage(self, team=None):
        user = self.env.user
        return (self.env.su or user.has_group(ADMIN) or user.has_group(PH)
                or (team and team.head_id == user))

    @api.model_create_multi
    def create(self, vals_list):
        seq = self.env['ir.sequence'].sudo()
        for vals in vals_list:
            if not self.env.su:
                if self.PROTECTED & {k for k in vals if k != 'name'} or vals.get('status', 'active') != 'active':
                    raise UserError(_("The status and history fields are managed by the workflow."))
                user = self.env.user
                if not vals.get('sales_team_id'):
                    team = user._otm_default_sales_team()
                    if team:
                        vals['sales_team_id'] = team.id
                team = self.env['otm.sales.team'].sudo().browse(vals.get('sales_team_id'))
                if team and not (user.has_group(ADMIN) or user.has_group(PH)
                                 or user in (team.head_id | team.member_ids)):
                    raise AccessError(_("You can only create services for your own sales team."))
                vals.setdefault('responsible_user_id', user.id)
            vals['name'] = seq.next_by_code('otm.client.service') or _('New')
        return super().create(vals_list)

    def write(self, vals):
        if not (self.env.su or self.env.context.get('otm_transition')):
            if self.PROTECTED & vals.keys():
                raise UserError(_("The status and history fields can only change through the workflow."))
            for s in self:
                if s.status == 'cancelled':
                    raise UserError(_("A cancelled service cannot be edited."))
                if not self._otm_can_manage(s.sales_team_id) and self.env.user != s.responsible_user_id:
                    raise AccessError(_("Only the responsible user, the Sales Head or a Project Head can edit this service."))
            if {'sales_team_id', 'responsible_user_id', 'customer_id', 'amount', 'renewal_amount',
                    'expiry_date', 'billing_type', 'renewal_period'} & vals.keys():
                for s in self:
                    if not self._otm_can_manage(s.sales_team_id) and self.env.user != s.responsible_user_id:
                        raise AccessError(_("You cannot change these fields."))
                if {'sales_team_id', 'responsible_user_id'} & vals.keys() and not all(
                        self._otm_can_manage(s.sales_team_id) for s in self):
                    raise AccessError(_("Only a Sales Head, a Project Head or an Administrator can reassign a service."))
        return super().write(vals)

    def unlink(self):
        if not self.env.su:
            raise UserError(_("Services cannot be deleted. Cancel them instead."))
        return super().unlink()

    # -- engine hooks -----------------------------------------------------
    def _otm_log_scope(self):
        return (self.sales_team_id, self.responsible_user_id)

    def _otm_check_actor(self, action):
        self.ensure_one()
        if self.env.su or self._otm_can_manage(self.sales_team_id):
            return
        raise AccessError(_("Only the Sales Head, a Project Head or an Administrator can cancel a service."))

    def _otm_after_transition(self, action, old_state, reason):
        for s in self:
            if action == 'system_expiring':
                s._otm_open_renewal_opportunity()
            if action == 'cancel':
                s.with_context(otm_transition=True).write({'cancel_reason': reason})
                s.renewal_ids.filtered(lambda r: r.status in ('follow_up', 'estimate_sent', 'approved')) \
                    .sudo()._otm_do_transition('cancel', reason=_("Service cancelled."))

    def action_cancel(self, reason=None):
        if not (reason or '').strip():
            raise UserError(_("A reason is required for this action."))
        return self._otm_do_transition('cancel', reason=reason)

    # -- reminders / status sync ---------------------------------------------
    def _otm_expiry_specs(self):
        return [('expiry_date', _("Service expiry"))]

    def _otm_expiry_domain(self):
        return [('status', 'in', ('active', 'renewed', 'expiring', 'expired'))]

    def _otm_reminder_user(self):
        self.ensure_one()
        return self.responsible_user_id or self.sales_team_id.head_id

    @api.model
    def _otm_after_expiry_sync(self, today, offsets):
        window = max(offsets)
        recs = self.search([('expiry_date', '!=', False),
                            ('status', 'in', ('active', 'renewed', 'expiring'))])
        for s in recs:
            days = (s.expiry_date - today).days
            if days < 0:
                s._otm_do_transition('system_expire')
            elif days <= window and s.status != 'expiring':
                s._otm_do_transition('system_expiring')

    def _otm_open_renewal_opportunity(self):
        """When a recurring service starts expiring, open the renewal (sales follow-up) automatically."""
        self.ensure_one()
        Renewal = self.env['otm.service.renewal'].sudo()
        if self.billing_type == 'one_time' or not self.renewal_period:
            return False
        if Renewal.search_count([('service_id', '=', self.id),
                                 ('status', 'in', ('follow_up', 'estimate_sent', 'approved', 'paid'))]):
            return False
        renewal = Renewal.create({'service_id': self.id})
        user = self.responsible_user_id or self.sales_team_id.head_id
        if user:
            self.sudo().activity_schedule(
                'mail.mail_activity_data_todo', date_deadline=fields.Date.context_today(self), user_id=user.id,
                summary=_("Renewal opportunity: %s", self.display_name),
                note=_("Renewal %(r)s was opened. Follow up with the customer.", r=renewal.name))
        return renewal

    def _otm_apply_renewal(self, renewal):
        """Only called from a completed renewal (payment received)."""
        self.ensure_one()
        today = fields.Date.context_today(self)
        base = max(self.expiry_date or today, today)
        months = PERIOD_MONTHS[renewal.renewal_period]
        new_expiry = add_period(base, months)
        old = self.expiry_date
        self._otm_do_transition('system_renew', extra_vals={
            'expiry_date': new_expiry, 'last_renewed_date': today,
            'amount': renewal.amount, 'start_date': self.start_date or today})
        self.message_post(body=_("Renewed for %(m)s month(s): expiry %(old)s → %(new)s (renewal %(r)s, %(a)s).",
                                 m=months, old=old or '-', new=new_expiry, r=renewal.name,
                                 a=renewal.currency_id.format(renewal.amount)))

    def _otm_email_partner(self):
        return self.customer_id

    def action_send_expiry_notice(self):
        self.ensure_one()
        if not self._otm_can_manage(self.sales_team_id) and self.env.user != self.responsible_user_id:
            raise AccessError(_("Only the responsible user, the Sales Head or a Project Head can send notices."))
        if not self.expiry_date:
            raise UserError(_("This service has no expiry date."))
        if not self.customer_id.email:
            raise UserError(_("The customer has no email address."))
        self._otm_send_mail('sales_project_lifecycle.mail_service_expiry')
        self.message_post(body=_("Expiry notice sent to %s.", self.customer_id.email))
        return True

    def action_start_renewal(self):
        self.ensure_one()
        Renewal = self.env['otm.service.renewal']
        if self.status == 'cancelled':
            raise UserError(_("A cancelled service cannot be renewed."))
        if self.billing_type == 'one_time' or not self.renewal_period:
            raise UserError(_("Only recurring services with a renewal period can be renewed."))
        if not self._otm_can_manage(self.sales_team_id) and self.env.user != self.responsible_user_id:
            raise AccessError(_("Only the responsible user, the Sales Head or a Project Head can start a renewal."))
        open_r = Renewal.sudo().search([('service_id', '=', self.id),
                                        ('status', 'in', ('follow_up', 'estimate_sent', 'approved', 'paid'))])
        if open_r:
            raise UserError(_("A renewal (%s) is already in progress for this service.", open_r[0].name))
        r = Renewal.sudo().create({'service_id': self.id})
        return {'type': 'ir.actions.act_window', 'res_model': 'otm.service.renewal', 'res_id': r.id,
                'view_mode': 'form'}

    def action_open_renewals(self):
        self.ensure_one()
        return {'type': 'ir.actions.act_window', 'name': _('Renewals'), 'res_model': 'otm.service.renewal',
                'view_mode': 'list,form', 'domain': [('service_id', '=', self.id)]}


class OtmServiceRenewal(models.Model):
    _name = 'otm.service.renewal'
    _description = 'Service Renewal'
    _inherit = ['mail.thread', 'otm.transition.mixin']
    _order = 'id desc'
    _otm_state_field = 'status'
    _otm_matrix = RENEWAL_MATRIX
    _otm_email_map = {'send_estimate': 'sales_project_lifecycle.mail_renewal'}

    def _otm_email_partner(self):
        return self.customer_id
    _otm_reason_methods = ('action_cancel',)

    name = fields.Char(readonly=True, copy=False, default=lambda s: _('New'))
    service_id = fields.Many2one('otm.client.service', required=True, index=True, ondelete='restrict', readonly=True)
    customer_id = fields.Many2one('res.partner', related='service_id.customer_id', store=True, index=True)
    sales_team_id = fields.Many2one('otm.sales.team', related='service_id.sales_team_id', store=True, index=True)
    responsible_user_id = fields.Many2one('res.users', related='service_id.responsible_user_id', store=True, index=True)
    company_id = fields.Many2one('res.company', related='service_id.company_id', store=True)
    currency_id = fields.Many2one('res.currency', related='company_id.currency_id')
    renewal_period = fields.Selection(RENEWAL_PERIOD, required=True, tracking=True)
    amount = fields.Monetary(currency_field='currency_id', tracking=True)
    old_expiry_date = fields.Date(readonly=True)
    status = fields.Selection([
        ('follow_up', 'Sales Follow-up'), ('estimate_sent', 'Renewal Estimate Sent'),
        ('approved', 'Customer Approved'), ('paid', 'Payment Received'),
        ('completed', 'Renewed'), ('cancelled', 'Cancelled')],
        default='follow_up', required=True, tracking=True, index=True, copy=False)
    estimate_note = fields.Text(string='Estimate / Follow-up Notes')
    approval_reference = fields.Char(string='Customer Approval Reference')
    payment_reference = fields.Char()
    payment_method = fields.Selection([('bank', 'Bank transfer'), ('upi', 'UPI'), ('cash', 'Cash'),
                                       ('cheque', 'Cheque'), ('online', 'Online')])
    paid_date = fields.Date()
    proof = fields.Binary(attachment=True)
    proof_filename = fields.Char()
    new_expiry_date = fields.Date(compute='_compute_new_expiry')
    cancel_reason = fields.Text(readonly=True, copy=False)

    PROTECTED = {'status', 'service_id', 'old_expiry_date', 'cancel_reason', 'name'}

    @api.depends('service_id.expiry_date', 'renewal_period')
    def _compute_new_expiry(self):
        today = fields.Date.context_today(self)
        for r in self:
            if r.service_id.expiry_date and r.renewal_period:
                r.new_expiry_date = add_period(max(r.service_id.expiry_date, today), PERIOD_MONTHS[r.renewal_period])
            else:
                r.new_expiry_date = False

    @api.model_create_multi
    def create(self, vals_list):
        if not self.env.su:
            raise UserError(_("Start a renewal from the service."))
        seq = self.env['ir.sequence'].sudo()
        for vals in vals_list:
            svc = self.env['otm.client.service'].browse(vals['service_id'])
            vals.setdefault('renewal_period', svc.renewal_period)
            vals.setdefault('amount', svc.renewal_amount or svc.amount)
            vals['old_expiry_date'] = svc.expiry_date
            vals['name'] = seq.next_by_code('otm.service.renewal') or _('New')
        return super().create(vals_list)

    def write(self, vals):
        if not (self.env.su or self.env.context.get('otm_transition')):
            if self.PROTECTED & vals.keys():
                raise UserError(_("These fields are managed by the workflow."))
            fin_only = {'payment_reference', 'payment_method', 'paid_date', 'proof', 'proof_filename'}
            for r in self:
                if r.status in ('completed', 'cancelled'):
                    raise UserError(_("A finished renewal cannot be edited."))
                if fin_only & vals.keys():
                    if not self.env.user.has_group(FIN) and not self.env.user.has_group(ADMIN):
                        raise AccessError(_("Only Finance can record payment details."))
                    if r.status != 'approved':
                        raise UserError(_("Payment details can only be entered once the customer approved."))
                elif r.status not in ('follow_up', 'estimate_sent') and {'amount', 'renewal_period'} & vals.keys():
                    raise UserError(_("The amount and period are locked after customer approval."))
                elif not r._otm_is_sales_side():
                    raise AccessError(_("Only the responsible user, the Sales Head or an Administrator can edit a renewal."))
        return super().write(vals)

    def unlink(self):
        if not self.env.su:
            raise UserError(_("Renewals cannot be deleted. Cancel them instead."))
        return super().unlink()

    def _otm_is_sales_side(self):
        self.ensure_one()
        u = self.env.user
        return (self.env.su or u.has_group(ADMIN) or u == self.responsible_user_id
                or u == self.sales_team_id.head_id)

    def _otm_log_scope(self):
        return (self.sales_team_id, self.responsible_user_id)

    def _otm_check_actor(self, action):
        self.ensure_one()
        u = self.env.user
        if self.env.su or u.has_group(ADMIN):
            return
        if action == 'confirm_payment':
            if not u.has_group(FIN):
                raise AccessError(_("Only Finance can confirm the renewal payment."))
        elif not self._otm_is_sales_side():
            raise AccessError(_("Only the responsible user or the Sales Head can do this."))

    def _otm_prerequisites(self, action):
        self.ensure_one()
        missing = []
        if action == 'send_estimate':
            if self.amount <= 0:
                missing.append(_("The renewal amount must be greater than zero."))
        if action == 'approve' and not (self.approval_reference or '').strip():
            missing.append(_("The customer approval reference is required."))
        if action == 'confirm_payment':
            if not (self.payment_reference or '').strip():
                missing.append(_("The payment reference is required."))
            if not self.paid_date:
                missing.append(_("The payment date is required."))
            elif self.paid_date > fields.Date.context_today(self):
                missing.append(_("The payment date cannot be in the future."))
            if not self.payment_method:
                missing.append(_("The payment method is required."))
            if not self.proof:
                missing.append(_("Upload the payment proof."))
        if action == 'renew' and self.service_id.status == 'cancelled':
            missing.append(_("The service has been cancelled."))
        return missing

    def _otm_after_transition(self, action, old_state, reason):
        for r in self:
            if action == 'renew':
                r.service_id.sudo()._otm_apply_renewal(r)
            elif action == 'cancel':
                r.with_context(otm_transition=True).write({'cancel_reason': reason})

    def action_send_estimate(self):
        return self._otm_do_transition('send_estimate')

    def action_approve(self):
        return self._otm_do_transition('approve')

    def action_confirm_payment(self):
        return self._otm_do_transition('confirm_payment')

    def action_renew(self):
        return self._otm_do_transition('renew')

    def action_cancel(self, reason=None):
        if not (reason or '').strip():
            raise UserError(_("A reason is required for this action."))
        return self._otm_do_transition('cancel', reason=reason)
