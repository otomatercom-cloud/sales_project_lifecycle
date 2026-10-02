from odoo import _, api, fields, models
from odoo.exceptions import ValidationError


class OtmLeadServiceLine(models.Model):
    _name = 'otm.lead.service.line'
    _description = 'Lead Service Line'
    _order = 'lead_id, sequence, id'

    lead_id = fields.Many2one(
        'otm.lead', string='Lead', required=True, ondelete='cascade', index=True)
    sequence = fields.Integer(default=10)
    service_id = fields.Many2one('otm.service', string='Service', required=True)
    quantity = fields.Float(default=1.0, digits='Product Unit')
    currency_id = fields.Many2one(
        'res.currency', related='lead_id.currency_id', string='Currency')
    base_unit_price = fields.Monetary(
        string='Base Unit Price', currency_field='currency_id',
        compute='_compute_base_unit_price', store=True, readonly=False, precompute=True)
    base_subtotal = fields.Monetary(
        string='Base Subtotal', currency_field='currency_id',
        compute='_compute_base_subtotal', store=True)
    requirement_description = fields.Text(string='Requirement')

    @api.depends('service_id')
    def _compute_base_unit_price(self):
        for line in self:
            if line.service_id:
                line.base_unit_price = line.service_id.base_amount

    @api.depends('quantity', 'base_unit_price', 'currency_id')
    def _compute_base_subtotal(self):
        for line in self:
            amount = line.quantity * line.base_unit_price
            line.base_subtotal = line.currency_id.round(amount) if line.currency_id else amount

    @api.constrains('quantity')
    def _check_quantity(self):
        for line in self:
            if line.quantity <= 0:
                raise ValidationError(_("The quantity must be greater than zero."))
