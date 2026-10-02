from odoo import _, api, fields, models
from odoo.exceptions import ValidationError


class OtmSalesTeam(models.Model):
    _name = 'otm.sales.team'
    _description = 'Sales Team'
    _inherit = ['mail.thread']
    _order = 'sequence, name'

    name = fields.Char(required=True, tracking=True)
    code = fields.Char(required=True, tracking=True)
    head_id = fields.Many2one(
        'res.users', string='Sales Head', required=True, tracking=True,
        index=True, domain=[('share', '=', False)])
    member_ids = fields.Many2many(
        'res.users', 'otm_sales_team_member_rel', 'team_id', 'user_id',
        string='Sales Executives', domain=[('share', '=', False)])
    active = fields.Boolean(default=True, tracking=True)
    description = fields.Text()
    company_id = fields.Many2one(
        'res.company', default=lambda self: self.env.company)
    currency_id = fields.Many2one(
        'res.currency', related='company_id.currency_id', string='Currency')
    target_amount = fields.Monetary(string='Monthly Sales Target', currency_field='currency_id')
    sequence = fields.Integer(default=10)
    lead_count = fields.Integer(string='Visible Leads', compute='_compute_lead_count')

    _code_uniq = models.Constraint('unique(code)', 'The team code must be unique.')

    @api.constrains('head_id', 'member_ids')
    def _check_head_not_member(self):
        for team in self:
            if team.head_id and team.head_id in team.member_ids:
                raise ValidationError(_(
                    "The Sales Head (%(head)s) cannot also be listed as a Sales Executive of the same team.",
                    head=team.head_id.name))

    def _compute_lead_count(self):
        # Runs as the current user, so the count only includes leads the
        # user is allowed to see (record rules apply).
        data = self.env['otm.lead']._read_group(
            [('sales_team_id', 'in', self.ids)], ['sales_team_id'], ['__count'])
        counts = {team.id: count for team, count in data}
        for team in self:
            team.lead_count = counts.get(team.id, 0)

    def write(self, vals):
        before = {}
        if 'member_ids' in vals:
            before = {t.id: t.member_ids.mapped('name') for t in self}
        res = super().write(vals)
        if 'member_ids' in vals:
            for team in self:
                old = ', '.join(before.get(team.id) or []) or '-'
                new = ', '.join(team.member_ids.mapped('name')) or '-'
                if old != new:
                    team.message_post(body=_(
                        "Sales Executives changed: %(old)s → %(new)s", old=old, new=new))
        return res

    def action_open_leads(self):
        self.ensure_one()
        return {
            'type': 'ir.actions.act_window',
            'name': _('Leads'),
            'res_model': 'otm.lead',
            'view_mode': 'list,form',
            'domain': [('sales_team_id', '=', self.id)],
            'context': {'default_sales_team_id': self.id},
        }
