from odoo import _, api, fields, models
from odoo.exceptions import AccessError, UserError

from .payment_schedule import TRIGGERS

FIN = 'sales_project_lifecycle.group_finance'
FIN_MGR = 'sales_project_lifecycle.group_finance_manager'
ADMIN = 'sales_project_lifecycle.group_lifecycle_admin'
FINANCE_FIELDS = {'payment_reference', 'paid_date', 'payment_method', 'notes', 'proof', 'proof_filename',
                  'message_main_attachment_id'}

PAYMENT_MATRIX = {
    'system_due': {'from': ('pending',), 'to': 'due', 'system': True},
    'request': {'from': ('due',), 'to': 'requested'},
    'confirm': {'from': ('due', 'requested'), 'to': 'received'},
    'cancel': {'from': ('pending', 'due', 'requested'), 'to': 'cancelled'},
    'system_cancel': {'from': ('pending', 'due', 'requested'), 'to': 'cancelled', 'system': True},
}
# payment trigger -> commission trigger that it fires once received
COMMISSION_EVENT = {'advance': 'advance_received', 'final_delivery': 'final_payment'}


class OtmDealPayment(models.Model):
    _name = 'otm.deal.payment'
    _description = 'Deal Payment Installment'
    _inherit = ['mail.thread', 'otm.transition.mixin']
    _order = 'deal_id, sequence, id'
    _otm_state_field = 'status'
    _otm_matrix = PAYMENT_MATRIX

    def _otm_email_partner(self):
        return self.deal_id.customer_id

    def _otm_template_xmlid(self, action):
        if action == 'request' and self.trigger == 'advance':
            return 'sales_project_lifecycle.mail_advance_request'
        if action == 'system_due' and self.trigger == 'after_training':
            return 'sales_project_lifecycle.mail_payment_30'
        if action == 'system_due' and self.trigger == 'final_delivery':
            return 'sales_project_lifecycle.mail_payment_final'
        return None
    _otm_reason_methods = ('action_cancel',)

    name = fields.Char(required=True, readonly=True)
    deal_id = fields.Many2one('otm.deal', required=True, index=True, ondelete='restrict', readonly=True)
    agreement_id = fields.Many2one('otm.customer.agreement', index=True, ondelete='restrict', readonly=True)
    sales_team_id = fields.Many2one('otm.sales.team', related='deal_id.sales_team_id', store=True, index=True)
    sales_head_id = fields.Many2one('res.users', related='deal_id.sales_head_id', store=True, index=True)
    salesperson_id = fields.Many2one('res.users', related='deal_id.salesperson_id', store=True, index=True)
    customer_id = fields.Many2one('res.partner', related='deal_id.customer_id', store=True)
    currency_id = fields.Many2one('res.currency', related='deal_id.currency_id')
    sequence = fields.Integer(default=10, readonly=True)
    percentage = fields.Float(digits=(16, 2), readonly=True)
    trigger = fields.Selection(TRIGGERS, required=True, readonly=True)
    amount = fields.Monetary(currency_field='currency_id', readonly=True)
    due_date = fields.Date(readonly=True, tracking=True)
    status = fields.Selection([
        ('pending', 'Not Due Yet'), ('due', 'Due'), ('requested', 'Requested'),
        ('received', 'Received'), ('cancelled', 'Cancelled'),
    ], default='pending', required=True, tracking=True, index=True, copy=False)
    paid_date = fields.Date(tracking=True, copy=False)
    payment_reference = fields.Char(copy=False, tracking=True)
    payment_method = fields.Selection([
        ('bank', 'Bank Transfer'), ('upi', 'UPI'), ('cheque', 'Cheque'),
        ('cash', 'Cash'), ('card', 'Card / Online')], copy=False)
    proof = fields.Binary(string='Payment Proof', attachment=True, copy=False)
    proof_filename = fields.Char(copy=False)
    confirmed_by_id = fields.Many2one('res.users', readonly=True, copy=False)
    confirmed_date = fields.Datetime(readonly=True, copy=False)
    notes = fields.Text()

    @api.model_create_multi
    def create(self, vals_list):
        if not self.env.su:
            raise UserError(_("Payment installments are generated from the agreement."))
        return super().create(vals_list)

    def write(self, vals):
        if not (self.env.su or self.env.context.get('otm_transition')):
            blocked = set(vals) - FINANCE_FIELDS
            if blocked:
                raise UserError(_("Payment installments can only change through the workflow."))
            for rec in self:
                if rec.status not in ('due', 'requested'):
                    raise UserError(_("Payment details can only be entered while the installment is due."))
                if not self.env.user.has_group(FIN):
                    raise AccessError(_("Only Finance can record payment details."))
        return super().write(vals)

    def unlink(self):
        if not self.env.su or self.filtered(lambda p: p.status not in ('pending', 'cancelled')):
            raise UserError(_("Payment installments cannot be deleted. Cancel them instead."))
        return super().unlink()

    def _otm_log_scope(self):
        return (self.sales_team_id, self.sales_head_id)

    def _otm_check_actor(self, action):
        self.ensure_one()
        user = self.env.user
        if self.env.su or user.has_group(ADMIN):
            return
        if action == 'request':
            if user in (self.sales_head_id | self.salesperson_id) or user.has_group(FIN):
                return
            raise AccessError(_("Only the salesperson, the Sales Head or Finance can request a payment."))
        if action == 'confirm' and not user.has_group(FIN):
            raise AccessError(_("Only Finance can confirm a payment."))
        if action == 'cancel' and not user.has_group(FIN_MGR):
            raise AccessError(_("Only a Finance Manager or an Administrator can cancel a payment."))

    def _otm_prerequisites(self, action):
        self.ensure_one()
        missing = []
        if action == 'confirm':
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
        return missing

    def _otm_after_transition(self, action, old_state, reason):
        for pay in self:
            if action == 'system_due':
                pay.with_context(otm_transition=True).write({'due_date': fields.Date.context_today(pay)})
                pay._otm_notify_finance()
            elif action == 'confirm':
                pay.with_context(otm_transition=True).write({
                    'confirmed_by_id': self.env.user.id, 'confirmed_date': fields.Datetime.now()})
                deal = pay.deal_id.sudo()
                deal._otm_refresh_payment_totals()
                event = COMMISSION_EVENT.get(pay.trigger)
                if event:
                    deal._otm_commission_event(event)
                if pay.trigger == 'advance':
                    deal._otm_after_advance_received()
                if pay.trigger == 'after_training':
                    deal.project_id._otm_sync_stages('pay30')
                if pay.trigger == 'final_delivery':
                    deal.project_id._otm_request_review()
                    deal.project_id._otm_sync_stages('pay20')

    def _otm_notify_finance(self):
        finance = self.env['res.users'].sudo().search([
            ('group_ids', 'in', self.env.ref('sales_project_lifecycle.group_finance').ids)])
        for pay in self:
            pay.message_post(
                subject=_("Payment due: %s", pay.name),
                body=_("%(name)s (%(amount)s) of deal %(deal)s is now due. Please collect it and confirm "
                       "receipt with the payment proof.",
                       name=pay.name, amount=pay.currency_id.format(pay.amount), deal=pay.deal_id.name),
                partner_ids=finance.partner_id.ids, message_type='notification',
                subtype_xmlid='mail.mt_comment')

    def _otm_require_reason(self, reason):
        if not (reason or '').strip():
            raise UserError(_("A reason is required for this action."))

    def action_request(self):
        return self._otm_do_transition('request')

    def action_confirm(self):
        return self._otm_do_transition('confirm')

    def action_cancel(self, reason=None):
        self._otm_require_reason(reason)
        return self._otm_do_transition('cancel', reason=reason)
