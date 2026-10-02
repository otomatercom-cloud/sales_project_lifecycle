from odoo import fields, models


class OtmLeadSource(models.Model):
    _name = 'otm.lead.source'
    _description = 'Lead Source'
    _order = 'sequence, name'

    name = fields.Char(required=True, translate=True)
    sequence = fields.Integer(default=10)
    active = fields.Boolean(default=True)

    _name_uniq = models.Constraint('unique(name)', 'The lead source name must be unique.')
