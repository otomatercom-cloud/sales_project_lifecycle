from odoo import _, api, fields, models
from odoo.exceptions import ValidationError


class OtmCommissionRule(models.Model):
    _name = 'otm.commission.rule'
    _description = 'Commission Rule'
    _order = 'sequence, id'

    name = fields.Char(required=True)
    sequence = fields.Integer(default=10)
    active = fields.Boolean(default=True)
    is_default = fields.Boolean(
        string='Default Rule', help="Used for services that have no rule of their own.")
    trigger = fields.Selection([
        ('advance_received', 'When the advance is received'),
        ('project_completed', 'When the project is completed'),
        ('final_payment', 'When the final payment is received'),
    ], string='Commission Becomes Earned', required=True, default='advance_received')
    percentage = fields.Float(
        string='% of Additional Amount', digits=(16, 2), default=100.0,
        help="Share of the additional selling amount that is paid as commission.")
    max_amount = fields.Monetary(
        string='Maximum Commission (per service line)', currency_field='currency_id',
        help="0 means no maximum.")
    approval_required = fields.Boolean(
        string='Approval Required', default=True,
        help="If unchecked, an earned commission is approved and credited to the wallet automatically.")
    company_id = fields.Many2one('res.company', default=lambda self: self.env.company)
    currency_id = fields.Many2one('res.currency', related='company_id.currency_id')

    @api.constrains('percentage', 'max_amount')
    def _check_values(self):
        for rule in self:
            if not 0 <= rule.percentage <= 100:
                raise ValidationError(_("The commission percentage must be between 0 and 100."))
            if rule.max_amount < 0:
                raise ValidationError(_("The maximum commission cannot be negative."))

    @api.constrains('is_default', 'active')
    def _check_single_default(self):
        for rule in self.filtered(lambda r: r.is_default and r.active):
            if self.search_count([('id', '!=', rule.id), ('is_default', '=', True)]) :
                raise ValidationError(_("Only one active default commission rule is allowed."))

    def _compute_commission(self, additional_subtotal):
        self.ensure_one()
        amount = additional_subtotal * self.percentage / 100.0
        if self.max_amount:
            amount = min(amount, self.max_amount)
        return self.currency_id.round(amount) if self.currency_id else amount

    @api.model
    def _resolve_for_service(self, service):
        return service.sudo().commission_rule_id or self.sudo().search(
            [('is_default', '=', True)], limit=1)
