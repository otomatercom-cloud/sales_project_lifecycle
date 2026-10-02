from odoo import _, api, fields, models
from odoo.exceptions import AccessError, UserError

FIN_MGR = 'sales_project_lifecycle.group_finance_manager'

TXN_SIGN = {
    'commission_credit': 1, 'commission_adjustment': 1,
    'commission_payout': -1, 'reversal': -1,
}


class OtmSalesWallet(models.Model):
    _name = 'otm.sales.wallet'
    _description = 'Sales Head Wallet'
    _order = 'sales_head_id'

    sales_head_id = fields.Many2one(
        'res.users', string='Sales Head', required=True, index=True, ondelete='restrict')
    name = fields.Char(related='sales_head_id.name', store=True)
    company_id = fields.Many2one('res.company', default=lambda self: self.env.company)
    currency_id = fields.Many2one('res.currency', related='company_id.currency_id')
    transaction_ids = fields.One2many('otm.sales.wallet.transaction', 'wallet_id')
    # Always derived from the ledger, never typed.
    balance = fields.Monetary(
        string='Available Balance', currency_field='currency_id',
        compute='_compute_balance', store=True)
    total_credited = fields.Monetary(currency_field='currency_id', compute='_compute_balance', store=True)
    total_paid = fields.Monetary(currency_field='currency_id', compute='_compute_balance', store=True)
    total_reversed = fields.Monetary(currency_field='currency_id', compute='_compute_balance', store=True)

    _head_unique = models.Constraint('unique(sales_head_id)', 'A Sales Head can only have one wallet.')

    @api.depends('transaction_ids.amount', 'transaction_ids.transaction_type')
    def _compute_balance(self):
        for wallet in self:
            credited = paid = reversed_ = 0.0
            for txn in wallet.transaction_ids:
                if txn.transaction_type in ('commission_credit', 'commission_adjustment'):
                    credited += txn.amount
                elif txn.transaction_type == 'commission_payout':
                    paid += txn.amount
                else:
                    reversed_ += txn.amount
            wallet.total_credited = credited
            wallet.total_paid = paid
            wallet.total_reversed = reversed_
            wallet.balance = credited - paid - reversed_

    @api.model_create_multi
    def create(self, vals_list):
        if not self.env.su:
            raise UserError(_("Wallets are created automatically."))
        return super().create(vals_list)

    def write(self, vals):
        if not self.env.su:
            raise UserError(_("A wallet balance cannot be edited. It is derived from the ledger."))
        return super().write(vals)

    def unlink(self):
        raise UserError(_("Wallets cannot be deleted."))

    @api.model
    def _get_or_create(self, head):
        wallet = self.sudo().search([('sales_head_id', '=', head.id)], limit=1)
        return wallet or self.sudo().create({'sales_head_id': head.id})

    def action_open_transactions(self):
        self.ensure_one()
        return {
            'type': 'ir.actions.act_window', 'name': _('Wallet Transactions'),
            'res_model': 'otm.sales.wallet.transaction', 'view_mode': 'list,form',
            'domain': [('wallet_id', '=', self.id)],
        }

    def action_add_adjustment(self, amount=0.0, notes=None):
        """Manual correction by a Finance Manager (positive adds, negative deducts)."""
        self.ensure_one()
        if not (self.env.su or self.env.user.has_group(FIN_MGR)):
            raise AccessError(_("Only a Finance Manager or an Administrator can adjust a wallet."))
        if not amount:
            raise UserError(_("The adjustment amount cannot be zero."))
        if not (notes or '').strip():
            raise UserError(_("A note is required for a wallet adjustment."))
        ttype = 'commission_adjustment' if amount > 0 else 'reversal'
        self.env['otm.sales.wallet.transaction']._post(
            None, ttype, abs(amount), notes, wallet=self)
        return True


class OtmSalesWalletTransaction(models.Model):
    """Append-only ledger."""
    _name = 'otm.sales.wallet.transaction'
    _description = 'Wallet Transaction'
    _order = 'id desc'

    wallet_id = fields.Many2one('otm.sales.wallet', required=True, index=True, ondelete='restrict')
    sales_head_id = fields.Many2one('res.users', related='wallet_id.sales_head_id', store=True, index=True)
    sales_team_id = fields.Many2one('otm.sales.team', index=True)
    transaction_type = fields.Selection([
        ('commission_credit', 'Commission Credit'),
        ('commission_adjustment', 'Adjustment'),
        ('commission_payout', 'Payout'),
        ('reversal', 'Reversal'),
    ], required=True, index=True)
    amount = fields.Monetary(currency_field='currency_id', required=True,
                             help="Always positive; the type decides the direction.")
    signed_amount = fields.Monetary(currency_field='currency_id', compute='_compute_signed', store=True)
    currency_id = fields.Many2one('res.currency', related='wallet_id.currency_id')
    commission_id = fields.Many2one('otm.sales.commission', index=True, ondelete='restrict')
    lead_id = fields.Many2one('otm.lead')
    customer_id = fields.Many2one('res.partner')
    date = fields.Datetime(default=fields.Datetime.now, required=True)
    status = fields.Selection([('posted', 'Posted')], default='posted', required=True)
    notes = fields.Text()
    user_id = fields.Many2one('res.users', string='Posted By', default=lambda s: s.env.user)

    @api.depends('amount', 'transaction_type')
    def _compute_signed(self):
        for txn in self:
            txn.signed_amount = txn.amount * TXN_SIGN.get(txn.transaction_type, 1)

    @api.model
    def _post(self, commission, ttype, amount, notes, wallet=None):
        wallet = wallet or self.env['otm.sales.wallet']._get_or_create(commission.sales_head_id)
        vals = {
            'wallet_id': wallet.id, 'transaction_type': ttype, 'amount': amount, 'notes': notes,
            'user_id': self.env.user.id,
        }
        if commission:
            vals.update({
                'commission_id': commission.id, 'lead_id': commission.lead_id.id,
                'customer_id': commission.customer_id.id, 'sales_team_id': commission.sales_team_id.id})
        return self.sudo().create(vals)

    @api.model_create_multi
    def create(self, vals_list):
        if not self.env.su:
            raise UserError(_("Wallet transactions are posted by the system only."))
        for vals in vals_list:
            if vals.get('amount', 0) <= 0:
                raise UserError(_("A wallet transaction amount must be positive."))
        return super().create(vals_list)

    def write(self, vals):
        raise UserError(_("Wallet transactions cannot be modified."))

    def unlink(self):
        raise UserError(_("Wallet transactions cannot be deleted."))
