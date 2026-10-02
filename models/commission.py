from odoo import _, api, fields, models
from odoo.exceptions import AccessError, UserError

FIN_MGR = 'sales_project_lifecycle.group_finance_manager'
ADMIN = 'sales_project_lifecycle.group_lifecycle_admin'

COMMISSION_MATRIX = {
    'earn': {'from': ('pending',), 'to': 'earned'},
    'system_earn': {'from': ('pending',), 'to': 'earned', 'system': True},
    'approve': {'from': ('earned',), 'to': 'approved'},
    'system_approve': {'from': ('earned',), 'to': 'approved', 'system': True},
    'pay': {'from': ('approved',), 'to': 'paid'},
    'cancel': {'from': ('pending', 'earned'), 'to': 'cancelled'},
    'system_cancel': {'from': ('pending', 'earned'), 'to': 'cancelled', 'system': True},
    'reverse': {'from': ('approved', 'paid'), 'to': 'reversed'},
}


class OtmSalesCommission(models.Model):
    _name = 'otm.sales.commission'
    _description = 'Sales Head Commission'
    _inherit = ['mail.thread', 'otm.transition.mixin']
    _order = 'id desc'
    _otm_state_field = 'status'
    _otm_matrix = COMMISSION_MATRIX
    _otm_reason_methods = ('action_earn', 'action_cancel', 'action_reverse')

    name = fields.Char(string='Reference', readonly=True, copy=False, default=lambda s: _('New'))
    deal_id = fields.Many2one('otm.deal', required=True, ondelete='restrict', index=True)
    deal_line_id = fields.Many2one('otm.deal.line', ondelete='set null')
    rule_id = fields.Many2one('otm.commission.rule', string='Rule Applied')
    trigger = fields.Selection([
        ('advance_received', 'Advance received'),
        ('project_completed', 'Project completed'),
        ('final_payment', 'Final payment received'),
    ], required=True, readonly=True)
    approval_required = fields.Boolean(readonly=True)
    sales_head_id = fields.Many2one('res.users', required=True, index=True, readonly=True)
    sales_team_id = fields.Many2one('otm.sales.team', index=True, readonly=True)
    lead_id = fields.Many2one('otm.lead', index=True, readonly=True)
    estimate_id = fields.Many2one('otm.estimate', readonly=True)
    customer_id = fields.Many2one('res.partner', readonly=True)
    service_id = fields.Many2one('otm.service', readonly=True)
    currency_id = fields.Many2one('res.currency', related='deal_id.currency_id')
    base_amount = fields.Monetary(currency_field='currency_id', readonly=True)
    additional_amount = fields.Monetary(currency_field='currency_id', readonly=True)
    additional_percentage = fields.Float(
        compute='_compute_percentages', store=True, digits=(16, 2))
    commission_amount = fields.Monetary(currency_field='currency_id', readonly=True, tracking=True)
    commission_percentage = fields.Float(
        string='Commission % of Base', compute='_compute_percentages', store=True, digits=(16, 2))
    status = fields.Selection([
        ('pending', 'Pending'), ('earned', 'Earned'), ('approved', 'Approved'),
        ('paid', 'Paid'), ('cancelled', 'Cancelled'), ('reversed', 'Reversed'),
    ], default='pending', required=True, tracking=True, index=True, copy=False)
    earned_date = fields.Datetime(readonly=True, copy=False)
    approved_date = fields.Datetime(readonly=True, copy=False)
    paid_date = fields.Datetime(readonly=True, copy=False)
    notes = fields.Text()
    transaction_ids = fields.One2many('otm.sales.wallet.transaction', 'commission_id')

    @api.depends('base_amount', 'additional_amount', 'commission_amount')
    def _compute_percentages(self):
        for rec in self:
            rec.additional_percentage = (
                rec.additional_amount / rec.base_amount * 100.0) if rec.base_amount else 0.0
            rec.commission_percentage = (
                rec.commission_amount / rec.base_amount * 100.0) if rec.base_amount else 0.0

    @api.model_create_multi
    def create(self, vals_list):
        if not self.env.su:
            raise UserError(_("Commissions are generated automatically when a deal is locked."))
        seq = self.env['ir.sequence'].sudo()
        for vals in vals_list:
            vals['name'] = seq.next_by_code('otm.commission') or _('New')
        return super().create(vals_list)

    def write(self, vals):
        if not (self.env.su or self.env.context.get('otm_transition')) \
                and set(vals) - {'notes', 'message_main_attachment_id'}:
            raise UserError(_("Commission values can only change through the workflow."))
        return super().write(vals)

    def unlink(self):
        raise UserError(_("Commissions cannot be deleted. Cancel or reverse them instead."))

    # -- hooks ----------------------------------------------------------
    def _otm_log_scope(self):
        return (self.sales_team_id, self.sales_head_id)

    def _otm_check_actor(self, action):
        self.ensure_one()
        user = self.env.user
        if self.env.su or user.has_group(FIN_MGR):
            return
        raise AccessError(_("Only a Finance Manager or an Administrator can do this."))

    def _otm_prerequisites(self, action):
        self.ensure_one()
        missing = []
        if action == 'pay':
            wallet = self.env['otm.sales.wallet']._get_or_create(self.sales_head_id)
            if wallet.balance < self.commission_amount:
                missing.append(_(
                    "The wallet balance (%(bal)s) is lower than the payout (%(amt)s).",
                    bal=wallet.balance, amt=self.commission_amount))
        return missing

    def _otm_after_transition(self, action, old_state, reason):
        now = fields.Datetime.now()
        Txn = self.env['otm.sales.wallet.transaction']
        for rec in self:
            if action in ('earn', 'system_earn'):
                rec.with_context(otm_transition=True).write({'earned_date': now})
            elif action in ('approve', 'system_approve'):
                rec.with_context(otm_transition=True).write({'approved_date': now})
                Txn._post(rec, 'commission_credit', rec.commission_amount, _("Commission approved"))
            elif action == 'pay':
                rec.with_context(otm_transition=True).write({'paid_date': now})
                Txn._post(rec, 'commission_payout', rec.commission_amount, reason or _("Commission paid"))
            elif action == 'reverse':
                Txn._post(rec, 'reversal', rec.commission_amount, reason or _("Commission reversed"))

    # -- public actions -------------------------------------------------
    def _otm_require_reason(self, reason):
        if not (reason or '').strip():
            raise UserError(_("A reason is required for this action."))

    def action_earn(self, reason=None):
        self._otm_require_reason(reason)
        return self._otm_do_transition('earn', reason=reason)

    def action_approve(self):
        return self._otm_do_transition('approve')

    def action_pay(self):
        return self._otm_do_transition('pay')

    def action_cancel(self, reason=None):
        self._otm_require_reason(reason)
        return self._otm_do_transition('cancel', reason=reason)

    def action_reverse(self, reason=None):
        self._otm_require_reason(reason)
        return self._otm_do_transition('reverse', reason=reason)
