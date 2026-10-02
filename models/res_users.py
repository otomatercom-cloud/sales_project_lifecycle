from odoo import _, api, fields, models
from odoo.exceptions import ValidationError


class ResUsers(models.Model):
    _inherit = 'res.users'

    otm_headed_team_ids = fields.One2many(
        'otm.sales.team', 'head_id', string='Teams Headed')
    otm_member_team_ids = fields.Many2many(
        'otm.sales.team', 'otm_sales_team_member_rel', 'user_id', 'team_id',
        string='Teams as Executive')
    otm_sales_team_ids = fields.Many2many(
        'otm.sales.team', compute='_compute_otm_sales_team_ids',
        compute_sudo=True, string='Sales Teams')
    otm_primary_sales_team_id = fields.Many2one(
        'otm.sales.team', string='Primary Sales Team')
    otm_sales_role = fields.Selection(
        [('sales_executive', 'Sales Executive'), ('sales_head', 'Sales Head')],
        compute='_compute_otm_sales_role', compute_sudo=True, string='Sales Role')

    @api.depends('otm_headed_team_ids', 'otm_member_team_ids')
    def _compute_otm_sales_team_ids(self):
        for user in self:
            user.otm_sales_team_ids = user.otm_headed_team_ids | user.otm_member_team_ids

    @api.depends('otm_headed_team_ids', 'otm_member_team_ids')
    def _compute_otm_sales_role(self):
        for user in self:
            if user.otm_headed_team_ids:
                user.otm_sales_role = 'sales_head'
            elif user.otm_member_team_ids:
                user.otm_sales_role = 'sales_executive'
            else:
                user.otm_sales_role = False

    @api.constrains('otm_primary_sales_team_id')
    def _check_otm_primary_sales_team(self):
        for user in self:
            team = user.otm_primary_sales_team_id
            if team and team not in user.otm_sales_team_ids:
                raise ValidationError(_(
                    "The primary sales team of %(user)s must be one of the teams the user belongs to.",
                    user=user.name))

    def _otm_default_sales_team(self):
        """Team used as default on new records: primary team if still valid, else the first team."""
        self.ensure_one()
        user = self.sudo()
        if user.otm_primary_sales_team_id and user.otm_primary_sales_team_id in user.otm_sales_team_ids:
            return user.otm_primary_sales_team_id
        return user.otm_sales_team_ids[:1]
