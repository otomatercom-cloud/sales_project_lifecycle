from odoo import _, api, fields, models
from odoo.exceptions import UserError, ValidationError

HEAD_GROUP = 'sales_project_lifecycle.group_sales_head'


class OtmEstimateLockMixin(models.AbstractModel):
    """Child rows of an estimate can only change while the estimate is editable."""
    _name = 'otm.estimate.lock.mixin'
    _description = 'Estimate Child Lock'

    def _otm_check_estimate_editable(self, estimates):
        if self.env.su:
            return
        for est in estimates.sudo():
            if est.status not in ('draft', 'internal_review'):
                raise UserError(_(
                    "Estimate %s is locked. Use 'Revise' to change its services or customizations.",
                    est.display_name))


class OtmEstimateLine(models.Model):
    _name = 'otm.estimate.line'
    _description = 'Estimate Line'
    _inherit = ['otm.estimate.lock.mixin']
    _order = 'estimate_id, sequence, id'

    estimate_id = fields.Many2one('otm.estimate', required=True, ondelete='cascade', index=True)
    sequence = fields.Integer(default=10)
    service_id = fields.Many2one('otm.service', string='Service', required=True)
    description = fields.Char(
        compute='_compute_from_service', store=True, readonly=False, precompute=True)
    currency_id = fields.Many2one('res.currency', related='estimate_id.currency_id')
    quantity = fields.Float(default=1.0, digits='Product Unit')
    base_unit_price = fields.Monetary(
        currency_field='currency_id', compute='_compute_from_service',
        store=True, readonly=False, precompute=True)
    # Decided by the Sales Head. Only Sales Heads / administrators can read or write it.
    additional_amount = fields.Monetary(
        string='Additional Amount (per unit)', currency_field='currency_id',
        groups=HEAD_GROUP)
    additional_percentage = fields.Float(
        compute='_compute_additional_percentage', digits=(16, 2), groups=HEAD_GROUP,
        help="Calculated: additional amount / base unit price x 100.")
    selling_unit_price = fields.Monetary(
        currency_field='currency_id', compute='_compute_prices', store=True)
    base_subtotal = fields.Monetary(
        currency_field='currency_id', compute='_compute_prices', store=True)
    additional_subtotal = fields.Monetary(
        currency_field='currency_id', compute='_compute_prices', store=True,
        groups=HEAD_GROUP)
    subtotal = fields.Monetary(
        string='Selling Subtotal', currency_field='currency_id',
        compute='_compute_prices', store=True)

    @api.depends('service_id')
    def _compute_from_service(self):
        for line in self:
            if line.service_id:
                line.description = line.service_id.name
                line.base_unit_price = line.service_id.base_amount

    @api.depends('additional_amount', 'base_unit_price')
    def _compute_additional_percentage(self):
        for line in self:
            line.additional_percentage = (
                line.additional_amount / line.base_unit_price * 100.0) if line.base_unit_price else 0.0

    @api.depends('quantity', 'base_unit_price', 'additional_amount', 'currency_id')
    def _compute_prices(self):
        for line in self:
            cur = line.currency_id
            rnd = cur.round if cur else (lambda x: x)
            line.selling_unit_price = line.base_unit_price + line.additional_amount
            line.base_subtotal = rnd(line.quantity * line.base_unit_price)
            line.additional_subtotal = rnd(line.quantity * line.additional_amount)
            line.subtotal = rnd(line.quantity * line.selling_unit_price)

    @api.constrains('quantity', 'base_unit_price', 'additional_amount')
    def _check_amounts(self):
        for line in self.sudo():
            if line.quantity <= 0:
                raise ValidationError(_("The quantity must be greater than zero."))
            if line.base_unit_price < 0:
                raise ValidationError(_("The base unit price cannot be negative."))
            if line.additional_amount < 0:
                raise ValidationError(_("The additional amount cannot be negative."))

    @api.model_create_multi
    def create(self, vals_list):
        self._otm_check_estimate_editable(
            self.env['otm.estimate'].browse([v['estimate_id'] for v in vals_list if v.get('estimate_id')]))
        return super().create(vals_list)

    def write(self, vals):
        self._otm_check_estimate_editable(self.mapped('estimate_id'))
        return super().write(vals)

    def unlink(self):
        self._otm_check_estimate_editable(self.mapped('estimate_id'))
        return super().unlink()


class OtmEstimateCustomization(models.Model):
    _name = 'otm.estimate.customization'
    _description = 'Estimate Customization'
    _inherit = ['otm.estimate.lock.mixin']
    _order = 'estimate_id, id'

    estimate_id = fields.Many2one('otm.estimate', required=True, ondelete='cascade', index=True)
    currency_id = fields.Many2one('res.currency', related='estimate_id.currency_id')
    customization_type = fields.Selection(
        [('percentage', 'Percentage'), ('fixed', 'Fixed Amount')],
        default='fixed', required=True)
    percentage = fields.Float(digits=(16, 2))
    fixed_amount = fields.Monetary(currency_field='currency_id')
    calculated_amount = fields.Monetary(
        currency_field='currency_id', compute='_compute_calculated_amount', store=True)
    description = fields.Text(required=True)
    requested_by_id = fields.Many2one(
        'res.users', string='Requested By', default=lambda self: self.env.user)
    requested_date = fields.Date(default=fields.Date.context_today)

    @api.depends('customization_type', 'percentage', 'fixed_amount', 'estimate_id.subtotal', 'currency_id')
    def _compute_calculated_amount(self):
        for rec in self:
            if rec.customization_type == 'percentage':
                amount = rec.estimate_id.subtotal * rec.percentage / 100.0
            else:
                amount = rec.fixed_amount
            rec.calculated_amount = rec.currency_id.round(amount) if rec.currency_id else amount

    @api.constrains('percentage', 'fixed_amount', 'description')
    def _check_values(self):
        for rec in self:
            if rec.percentage < 0 or rec.fixed_amount < 0:
                raise ValidationError(_("Customization amounts cannot be negative."))
            if not (rec.description or '').strip():
                raise ValidationError(_("A customization description is mandatory."))

    @api.model_create_multi
    def create(self, vals_list):
        self._otm_check_estimate_editable(
            self.env['otm.estimate'].browse([v['estimate_id'] for v in vals_list if v.get('estimate_id')]))
        return super().create(vals_list)

    def write(self, vals):
        self._otm_check_estimate_editable(self.mapped('estimate_id'))
        return super().write(vals)

    def unlink(self):
        self._otm_check_estimate_editable(self.mapped('estimate_id'))
        return super().unlink()
