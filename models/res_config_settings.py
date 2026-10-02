from odoo import _, api, fields, models
from odoo.exceptions import ValidationError

from .expiry import DEFAULT_REMINDER_DAYS, parse_reminder_days


class ResConfigSettings(models.TransientModel):
    _inherit = 'res.config.settings'

    otm_executive_discount_limit = fields.Float(
        string='Sales Executive Discount Limit (%)', digits=(16, 2),
        config_parameter='sales_project_lifecycle.executive_discount_limit', default=5.0,
        help="Discounts up to this percentage need no approval.")
    otm_head_discount_limit = fields.Float(
        string='Sales Head Discount Limit (%)', digits=(16, 2),
        config_parameter='sales_project_lifecycle.head_discount_limit', default=15.0,
        help="Discounts above this percentage need Administrator approval.")
    otm_default_tax_percent = fields.Float(
        string='Default Estimate Tax (%)', digits=(16, 2),
        config_parameter='sales_project_lifecycle.default_tax_percent', default=0.0)
    otm_lost_description_required = fields.Boolean(
        string='Require Lost Description',
        config_parameter='sales_project_lifecycle.lost_description_required')
    otm_agreement_terms = fields.Text(string='Default Agreement Terms')

    @api.model
    def get_values(self):
        res = super().get_values()
        res['otm_agreement_terms'] = self.env['ir.config_parameter'].sudo().get_param(
            'sales_project_lifecycle.agreement_terms') or False
        return res

    def set_values(self):
        super().set_values()
        self.env['ir.config_parameter'].sudo().set_param(
            'sales_project_lifecycle.agreement_terms', self.otm_agreement_terms or '')

    otm_reminder_days = fields.Char(
        string='Expiry Reminder Days', default=DEFAULT_REMINDER_DAYS,
        config_parameter='sales_project_lifecycle.reminder_days',
        help="Comma separated days before expiry, 0 = on expiry. Example: 60,30,15,7,1,0")

    @api.constrains('otm_reminder_days')
    def _check_otm_reminder_days(self):
        for rec in self:
            try:
                if not parse_reminder_days(rec.otm_reminder_days):
                    raise ValueError
            except ValueError:
                raise ValidationError(_("Enter whole numbers separated by commas, for example 60,30,15,7,1,0."))

    otm_followup_days = fields.Integer(
        string='Follow-up After (days)', default=3, config_parameter='sales_project_lifecycle.followup_days',
        help="Estimates, payments and agreements waiting this long raise a follow-up activity.")

    otm_auto_create_project = fields.Boolean(
        string='Create Project Automatically', config_parameter='sales_project_lifecycle.auto_create_project',
        help="When the agreement is completed and the advance is received, the delivery project is "
             "created automatically instead of waiting for the Sales Head to press 'Create Project'.")
