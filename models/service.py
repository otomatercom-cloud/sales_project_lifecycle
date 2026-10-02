from odoo import _, api, fields, models
from odoo.exceptions import ValidationError


class OtmService(models.Model):
    _name = 'otm.service'
    _description = 'Service / Module Master'
    _order = 'name'

    name = fields.Char(required=True)
    code = fields.Char(required=True)
    description = fields.Text()
    service_type = fields.Selection([
        ('module', 'Software Module'),
        ('website', 'Website'),
        ('mobile_app', 'Mobile App'),
        ('integration', 'Integration'),
        ('hosting', 'Hosting / Server'),
        ('domain', 'Domain'),
        ('ssl', 'SSL'),
        ('amc', 'AMC / Support'),
        ('marketing', 'Digital Marketing'),
        ('other', 'Other'),
    ], required=True, default='module')
    company_id = fields.Many2one('res.company', default=lambda self: self.env.company)
    currency_id = fields.Many2one(
        'res.currency', required=True,
        default=lambda self: self.env.company.currency_id)
    # Reference amount only. NOT the customer selling price.
    base_amount = fields.Monetary(
        string='Base Amount', currency_field='currency_id',
        help="Reference amount of this service. The customer selling price is "
             "base amount + additional selling amount decided by the Sales Head.")
    active = fields.Boolean(default=True)
    recurring = fields.Boolean()
    recurring_period = fields.Selection([
        ('monthly', 'Monthly'),
        ('quarterly', 'Quarterly'),
        ('half_yearly', 'Half-Yearly'),
        ('yearly', 'Yearly'),
    ])
    default_duration = fields.Integer(string='Default Duration (days)')
    default_payment_terms = fields.Char(string='Default Payment Terms')
    commission_enabled = fields.Boolean(string='Commission Enabled', default=True)
    commission_rule_id = fields.Many2one(
        'otm.commission.rule', string='Commission Rule',
        help="Leave empty to use the default commission rule.")

    _code_uniq = models.Constraint('unique(code)', 'The service code must be unique.')
    _base_amount_positive = models.Constraint(
        'CHECK(base_amount >= 0)', 'The base amount cannot be negative.')

    @api.constrains('recurring', 'recurring_period')
    def _check_recurring_period(self):
        for service in self:
            if service.recurring and not service.recurring_period:
                raise ValidationError(_("Please select a recurring period for %s.", service.name))

    @api.depends('name', 'code')
    def _compute_display_name(self):
        for service in self:
            service.display_name = f"[{service.code}] {service.name}" if service.code else service.name
