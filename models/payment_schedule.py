from odoo import _, api, fields, models
from odoo.exceptions import ValidationError

TRIGGERS = [
    ('advance', 'Advance (before development)'),
    ('after_training', 'After training'),
    ('final_delivery', 'After final delivery'),
]


class OtmPaymentSchedule(models.Model):
    """Configurable payment schedule template (default: 50 / 30 / 20)."""
    _name = 'otm.payment.schedule'
    _description = 'Payment Schedule'
    _order = 'sequence, id'

    name = fields.Char(required=True)
    sequence = fields.Integer(default=10)
    active = fields.Boolean(default=True)
    is_default = fields.Boolean(string='Default Schedule')
    line_ids = fields.One2many('otm.payment.schedule.line', 'schedule_id', string='Installments', copy=True)
    total_percentage = fields.Float(compute='_compute_total', store=True, digits=(16, 2))

    @api.depends('line_ids.percentage')
    def _compute_total(self):
        for sch in self:
            sch.total_percentage = sum(sch.line_ids.mapped('percentage'))

    @api.constrains('line_ids', 'total_percentage')
    def _check_schedule(self):
        for sch in self:
            if not sch.line_ids:
                continue
            if abs(sch.total_percentage - 100.0) > 0.005:
                raise ValidationError(_(
                    "The installments of '%(name)s' must total 100%% (currently %(total).2f%%).",
                    name=sch.name, total=sch.total_percentage))
            if not sch.line_ids.filtered(lambda l: l.trigger == 'advance'):
                raise ValidationError(_("A payment schedule needs an advance installment."))

    @api.constrains('is_default', 'active')
    def _check_default(self):
        for sch in self.filtered(lambda s: s.is_default and s.active):
            if self.search_count([('id', '!=', sch.id), ('is_default', '=', True)]):
                raise ValidationError(_("Only one active default payment schedule is allowed."))

    @api.model
    def _get_default(self):
        return self.sudo().search([('is_default', '=', True)], limit=1)


class OtmPaymentScheduleLine(models.Model):
    _name = 'otm.payment.schedule.line'
    _description = 'Payment Schedule Line'
    _order = 'schedule_id, sequence, id'

    schedule_id = fields.Many2one('otm.payment.schedule', required=True, ondelete='cascade')
    sequence = fields.Integer(default=10)
    name = fields.Char(required=True)
    percentage = fields.Float(digits=(16, 2), required=True)
    trigger = fields.Selection(TRIGGERS, required=True, default='advance')

    @api.constrains('percentage')
    def _check_pct(self):
        for line in self:
            if not 0 < line.percentage <= 100:
                raise ValidationError(_("Each installment must be between 0 and 100%."))
