import base64

from odoo import _, api, fields, models
from odoo.exceptions import AccessError, UserError

ADMIN = 'sales_project_lifecycle.group_lifecycle_admin'
ACTIVE = ('draft', 'generated', 'sent', 'customer_accepted', 'signed', 'completed')

AGREEMENT_MATRIX = {
    'generate': {'from': ('draft',), 'to': 'generated'},
    'send': {'from': ('generated',), 'to': 'sent'},
    'accept': {'from': ('sent',), 'to': 'customer_accepted'},
    'sign': {'from': ('customer_accepted',), 'to': 'signed'},
    'complete': {'from': ('signed',), 'to': 'completed'},
    'cancel': {'from': ('draft', 'generated', 'sent', 'customer_accepted', 'signed'), 'to': 'cancelled'},
    'system_cancel': {
        'from': ('draft', 'generated', 'sent', 'customer_accepted', 'signed'), 'to': 'cancelled',
        'system': True},
}
EDITABLE_FIELDS = {
    'draft': {'scope', 'deliverables', 'terms', 'notes', 'payment_schedule_id', 'timeline_days',
              'message_main_attachment_id'},
}
ALWAYS = {'notes', 'message_main_attachment_id', 'customer_acceptance', 'signed_document',
          'signed_filename', 'signed_date'}


class OtmCustomerAgreement(models.Model):
    _name = 'otm.customer.agreement'
    _description = 'Customer Agreement'
    _inherit = ['mail.thread', 'mail.activity.mixin', 'otm.transition.mixin']
    _order = 'id desc'
    _rec_name = 'agreement_number'
    _otm_state_field = 'status'
    _otm_matrix = AGREEMENT_MATRIX
    _otm_email_map = {'send': 'sales_project_lifecycle.mail_agreement'}

    def _otm_email_partner(self):
        return self.customer_id
    _otm_reason_methods = ('action_cancel',)

    agreement_number = fields.Char(readonly=True, copy=False, index=True, default=lambda s: _('New'))
    deal_id = fields.Many2one('otm.deal', required=True, index=True, ondelete='restrict', readonly=True)
    lead_id = fields.Many2one('otm.lead', related='deal_id.lead_id', store=True, index=True)
    estimate_id = fields.Many2one('otm.estimate', related='deal_id.estimate_id', store=True)
    customer_id = fields.Many2one('res.partner', related='deal_id.customer_id', store=True, index=True)
    sales_team_id = fields.Many2one('otm.sales.team', related='deal_id.sales_team_id', store=True, index=True)
    sales_head_id = fields.Many2one('res.users', related='deal_id.sales_head_id', store=True, index=True)
    salesperson_id = fields.Many2one('res.users', related='deal_id.salesperson_id', store=True, index=True)
    company_id = fields.Many2one('res.company', related='deal_id.company_id', store=True)
    currency_id = fields.Many2one('res.currency', related='deal_id.currency_id')
    status = fields.Selection([
        ('draft', 'Draft'), ('generated', 'Generated'), ('sent', 'Sent'),
        ('customer_accepted', 'Customer Accepted'), ('signed', 'Signed'),
        ('completed', 'Completed'), ('cancelled', 'Cancelled'),
    ], default='draft', required=True, tracking=True, index=True, copy=False)
    scope = fields.Text(required=True)
    deliverables = fields.Text(required=True)
    terms = fields.Text(
        string='Terms & Conditions',
        default=lambda self: self.env['ir.config_parameter'].sudo().get_param(
            'sales_project_lifecycle.agreement_terms', ''))
    timeline_days = fields.Integer(string='Project Timeline (days)')
    approved_services = fields.Text(readonly=True, copy=False)
    approved_customizations = fields.Text(readonly=True, copy=False)
    final_amount = fields.Monetary(
        currency_field='currency_id', readonly=True, copy=False,
        groups='sales_project_lifecycle.group_sales_head,sales_project_lifecycle.group_finance')
    project_id = fields.Many2one('project.project', string='Project', readonly=True, copy=False)
    payment_schedule_id = fields.Many2one(
        'otm.payment.schedule', string='Payment Schedule',
        default=lambda self: self.env['otm.payment.schedule']._get_default())
    payment_ids = fields.One2many('otm.deal.payment', 'agreement_id', string='Payment Schedule Lines')
    agreement_date = fields.Date(readonly=True, copy=False)
    sent_date = fields.Datetime(readonly=True, copy=False)
    accepted_date = fields.Datetime(readonly=True, copy=False)
    customer_acceptance = fields.Text(
        string='Customer Acceptance', help="Who accepted and how (e-mail, signed copy, call...).")
    signed_date = fields.Date()
    signed_document = fields.Binary(string='Signed Agreement', attachment=True, copy=False)
    signed_filename = fields.Char()
    completed_date = fields.Datetime(readonly=True, copy=False)
    notes = fields.Text()

    # ------------------------------------------------------------------
    @api.model_create_multi
    def create(self, vals_list):
        seq = self.env['ir.sequence'].sudo()
        for vals in vals_list:
            if vals.get('status', 'draft') != 'draft' and not self.env.su:
                raise UserError(_("A new agreement must start as Draft."))
            vals['agreement_number'] = seq.next_by_code('otm.agreement') or _('New')
        return super().create(vals_list)

    def write(self, vals):
        if not (self.env.su or self.env.context.get('otm_transition')):
            if 'status' in vals:
                raise UserError(_("The agreement status cannot be edited directly. Use the workflow buttons."))
            for rec in self:
                allowed = EDITABLE_FIELDS.get(rec.status, set()) | ALWAYS
                if rec.status in ('completed', 'cancelled'):
                    allowed = {'notes', 'message_main_attachment_id'}
                if rec.status in ('generated', 'sent') and set(vals) & {'scope', 'deliverables'}:
                    raise UserError(_("The agreement content is locked once generated. Cancel and recreate it to change it."))
                if set(vals) - allowed:
                    raise UserError(_("These fields cannot be changed while the agreement is '%s'.",
                                      rec._otm_state_label(rec.status)))
        return super().write(vals)

    def unlink(self):
        if not self.env.su:
            raise UserError(_("Agreements cannot be deleted. Cancel the agreement instead."))
        return super().unlink()

    @api.depends('agreement_number')
    def _compute_display_name(self):
        for rec in self:
            rec.display_name = rec.agreement_number or _('New')

    # -- hooks ----------------------------------------------------------
    def _otm_log_scope(self):
        return (self.sales_team_id, self.sales_head_id)

    def _otm_check_actor(self, action):
        self.ensure_one()
        user = self.env.user
        if self.env.su or user.has_group(ADMIN):
            return
        if action in ('complete', 'cancel'):
            allowed = self.sales_head_id
        else:
            allowed = self.sales_head_id | self.salesperson_id
        if user not in allowed:
            raise AccessError(_("You are not allowed to perform this action on this agreement."))

    def _otm_prerequisites(self, action):
        self.ensure_one()
        missing = []
        deal = self.deal_id.sudo()
        if action == 'generate':
            if deal.status != 'locked':
                missing.append(_("The deal must be locked."))
            if not (self.scope or '').strip():
                missing.append(_("The scope is empty."))
            if not (self.deliverables or '').strip():
                missing.append(_("The deliverables are empty."))
            if not self.payment_schedule_id:
                missing.append(_("A payment schedule must be selected."))
            if not self.customer_id:
                missing.append(_("The customer is not set."))
            other = self.sudo().search([
                ('deal_id', '=', deal.id), ('id', '!=', self.id), ('status', 'in', ACTIVE)], limit=1)
            if other:
                missing.append(_("The deal already has an active agreement (%s).", other.agreement_number))
        if action == 'accept' and not (self.customer_acceptance or '').strip():
            missing.append(_("Record how the customer accepted the agreement."))
        if action == 'sign':
            if not self.signed_document:
                missing.append(_("Upload the signed agreement document."))
            if not self.signed_date:
                missing.append(_("The signed date is required."))
        if action == 'complete' and not self.signed_document:
            missing.append(_("The signed agreement document is missing."))
        if action == 'cancel' and self.sudo().payment_ids.filtered(lambda p: p.status == 'received'):
            missing.append(_("A payment has already been received; the agreement can no longer be cancelled."))
        return missing

    def _otm_build_payments(self):
        Pay = self.env['otm.deal.payment'].sudo()
        for ag in self:
            ag.sudo().payment_ids.unlink()
            lines = ag.payment_schedule_id.line_ids.sorted('sequence')
            total, assigned = ag.sudo().final_amount, 0.0
            for i, sl in enumerate(lines):
                if i == len(lines) - 1:
                    amount = total - assigned
                else:
                    amount = ag.currency_id.round(total * sl.percentage / 100.0)
                assigned += amount
                Pay.create({
                    'name': sl.name, 'deal_id': ag.deal_id.id, 'agreement_id': ag.id,
                    'sequence': sl.sequence, 'percentage': sl.percentage,
                    'trigger': sl.trigger, 'amount': amount})

    def _otm_after_transition(self, action, old_state, reason):
        now = fields.Datetime.now()
        for ag in self:
            deal = ag.deal_id.sudo()
            lead = ag.lead_id.sudo()
            w = ag.sudo().with_context(otm_transition=True)  # authorised transition; amount fields are restricted
            if action == 'complete':
                deal._otm_try_auto_project()
            if action == 'generate':
                svc = '\n'.join(
                    f"{l.description or l.service_id.name} x {l.quantity:g}"
                    for l in deal.line_ids)
                cust = '\n'.join(
                    f"{c.description} ({ag.currency_id.format(c.calculated_amount)})"
                    for c in deal.estimate_id.sudo().customization_ids)
                w.write({
                    'final_amount': deal.total_amount, 'approved_services': svc,
                    'approved_customizations': cust, 'agreement_date': fields.Date.context_today(ag)})
                ag._otm_build_payments()
                if lead.stage == 'deal_locked':
                    lead._otm_do_transition('system_agreement')
            elif action == 'send':
                w.write({'sent_date': now})
            elif action == 'accept':
                w.write({'accepted_date': now})
            elif action == 'sign' and ag.customer_id:
                self.env['ir.attachment'].sudo().create({
                    'name': ag.signed_filename or f"{ag.agreement_number}-signed",
                    'datas': ag.signed_document, 'res_model': 'res.partner',
                    'res_id': ag.customer_id.id})
            elif action == 'complete':
                w.write({'completed_date': now})
                deal._otm_payment_event('advance')
                if lead.stage == 'agreement':
                    lead._otm_do_transition('system_advance_pending')
            elif action in ('cancel', 'system_cancel'):
                ag.sudo().payment_ids._otm_do_transition(
                    'system_cancel', reason=reason or _('Agreement cancelled'))
                if lead.stage in ('agreement', 'advance_pending'):
                    lead._otm_do_transition('system_agreement_cancel')

    # -- public actions -------------------------------------------------
    def _otm_require_reason(self, reason):
        if not (reason or '').strip():
            raise UserError(_("A reason is required for this action."))

    def action_generate(self):
        return self._otm_do_transition('generate')

    def action_send(self):
        return self._otm_do_transition('send')

    def action_accept(self):
        return self._otm_do_transition('accept')

    def action_sign(self):
        return self._otm_do_transition('sign')

    def action_complete(self):
        return self._otm_do_transition('complete')

    def action_cancel(self, reason=None):
        self._otm_require_reason(reason)
        return self._otm_do_transition('cancel', reason=reason)

    def action_print(self):
        self.ensure_one()
        if self.status == 'draft':
            raise UserError(_("Generate the agreement before printing it."))
        return self.env.ref('sales_project_lifecycle.action_report_agreement').report_action(self)
