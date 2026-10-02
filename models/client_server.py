from odoo import _, api, fields, models
from odoo.exceptions import AccessError, UserError, ValidationError

ADMIN = 'sales_project_lifecycle.group_lifecycle_admin'
PH = 'sales_project_lifecycle.group_project_head'


class OtmClientServer(models.Model):
    _name = 'otm.client.server'
    _description = 'Client Server'
    _inherit = ['mail.thread', 'mail.activity.mixin', 'otm.expiry.mixin']
    _order = 'customer_id, name'

    name = fields.Char(required=True, tracking=True)
    customer_id = fields.Many2one('res.partner', required=True, index=True, tracking=True)
    provider = fields.Char()
    server_type = fields.Selection([
        ('vps', 'VPS'), ('dedicated', 'Dedicated'), ('cloud', 'Cloud'),
        ('shared', 'Shared hosting'), ('onprem', 'On-premise')], default='vps')
    ip_address = fields.Char()
    hostname = fields.Char()
    operating_system = fields.Char()
    cpu = fields.Char()
    ram = fields.Char()
    storage = fields.Char()
    database = fields.Char()
    domain = fields.Char()
    hosting_start_date = fields.Date()
    hosting_expiry_date = fields.Date(tracking=True)
    backup_schedule = fields.Char()
    ssl_expiry_date = fields.Date(tracking=True)
    status = fields.Selection([
        ('active', 'Active'), ('suspended', 'Suspended'), ('expired', 'Expired'),
        ('decommissioned', 'Decommissioned')], default='active', required=True, tracking=True)
    credential_reference = fields.Char(
        help="Where the credentials are kept (e.g. the password-manager entry name). "
             "Never enter the password itself.")
    responsible_id = fields.Many2one('res.users', string='Responsible', tracking=True, domain=[('share', '=', False)])
    notes = fields.Text()
    deployment_ids = fields.One2many('otm.deployment', 'server_id')
    active = fields.Boolean(default=True)

    @api.constrains('hosting_start_date', 'hosting_expiry_date')
    def _check_dates(self):
        for s in self:
            if s.hosting_start_date and s.hosting_expiry_date and s.hosting_expiry_date < s.hosting_start_date:
                raise ValidationError(_("The hosting expiry date cannot be before the start date."))

    @api.constrains('credential_reference')
    def _check_no_password(self):
        for s in self:
            ref = (s.credential_reference or '').lower()
            if any(w in ref for w in ('password:', 'passwd=', 'pwd=', 'pass:')):
                raise ValidationError(_("Do not store passwords here, only a reference to the vault entry."))

    def write(self, vals):
        if not (self.env.su or self.env.user.has_group(PH) or self.env.user.has_group(ADMIN)):
            raise AccessError(_("Only a Project Head or an Administrator can change a client server."))
        return super().write(vals)

    @api.model_create_multi
    def create(self, vals_list):
        if not (self.env.su or self.env.user.has_group(PH) or self.env.user.has_group(ADMIN)):
            raise AccessError(_("Only a Project Head or an Administrator can create a client server."))
        return super().create(vals_list)

    def _otm_expiry_specs(self):
        return [('hosting_expiry_date', _("Hosting expiry")), ('ssl_expiry_date', _("SSL expiry"))]

    def _otm_expiry_domain(self):
        return [('status', 'in', ('active', 'suspended', 'expired'))]

    def _otm_reminder_user(self):
        self.ensure_one()
        if self.responsible_id:
            return self.responsible_id
        return self.env['res.users'].sudo().search([
            ('group_ids', 'in', self.env.ref(PH).ids)], limit=1)
