from odoo import _, api, fields, models
from odoo.exceptions import AccessError, UserError, ValidationError

LOST_FROM = ('new', 'contacted', 'requirement', 'demo', 'estimate', 'negotiation',
             'deal_locked', 'agreement', 'advance_pending')

# Lead state-transition matrix. 'system' transitions are driven by later
# lifecycle phases (deal lock, agreement, advance, project) and have no
# user-callable button.
LEAD_MATRIX = {
    'contact': {'from': ('new',), 'to': 'contacted'},
    'collect_requirement': {'from': ('contacted',), 'to': 'requirement'},
    'demo': {'from': ('requirement',), 'to': 'demo'},
    'estimate': {'from': ('demo',), 'to': 'estimate'},
    'negotiate': {'from': ('estimate',), 'to': 'negotiation'},
    'lose': {'from': LOST_FROM, 'to': 'lost'},
    'reopen': {'from': ('lost',), 'to': 'new'},
    'system_deal_lock': {'from': ('negotiation',), 'to': 'deal_locked', 'system': True},
    'system_deal_unlock': {
        'from': ('deal_locked', 'agreement', 'advance_pending'), 'to': 'negotiation', 'system': True},
    'system_agreement_cancel': {
        'from': ('agreement', 'advance_pending'), 'to': 'deal_locked', 'system': True},
    'system_agreement': {'from': ('deal_locked',), 'to': 'agreement', 'system': True},
    'system_advance_pending': {'from': ('agreement',), 'to': 'advance_pending', 'system': True},
    'system_project': {'from': ('advance_pending',), 'to': 'project', 'system': True},
    'system_won': {'from': ('project',), 'to': 'won', 'system': True},
}


class OtmLead(models.Model):
    _name = 'otm.lead'
    _description = 'Sales Lead'
    _inherit = ['mail.thread', 'mail.activity.mixin', 'otm.transition.mixin']
    _otm_matrix = LEAD_MATRIX
    _order = 'priority desc, id desc'

    reference = fields.Char(readonly=True, copy=False, index=True)
    name = fields.Char(string='Lead Title', required=True, tracking=True)
    customer_id = fields.Many2one('res.partner', string='Customer', tracking=True, index=True)
    company_name = fields.Char(string='Company Name')
    contact_number = fields.Char()
    whatsapp_number = fields.Char(string='WhatsApp Number')
    email = fields.Char()
    location = fields.Char()
    lead_source_id = fields.Many2one('otm.lead.source', string='Lead Source', tracking=True)

    sales_team_id = fields.Many2one(
        'otm.sales.team', string='Sales Team', required=True, tracking=True, index=True,
        default=lambda self: self.env.user._otm_default_sales_team())
    sales_head_id = fields.Many2one(
        'res.users', string='Sales Head', related='sales_team_id.head_id',
        store=True, readonly=True, index=True)
    salesperson_id = fields.Many2one(
        'res.users', string='Salesperson', required=True, tracking=True, index=True,
        default=lambda self: self.env.user, domain=[('share', '=', False)])

    requirement_description = fields.Text(string='Requirement')
    company_id = fields.Many2one('res.company', default=lambda self: self.env.company)
    currency_id = fields.Many2one(
        'res.currency', related='company_id.currency_id', string='Currency')
    expected_budget = fields.Monetary(currency_field='currency_id')
    lead_quality = fields.Selection([
        ('hot', 'Hot'), ('warm', 'Warm'), ('cold', 'Cold'), ('maybe_later', 'Maybe Later'),
    ], default='warm', required=True, tracking=True)
    stage = fields.Selection([
        ('new', 'New'),
        ('contacted', 'Contacted'),
        ('requirement', 'Requirement'),
        ('demo', 'Demo'),
        ('estimate', 'Estimate'),
        ('negotiation', 'Negotiation'),
        ('deal_locked', 'Deal Locked'),
        ('agreement', 'Agreement'),
        ('advance_pending', 'Advance Pending'),
        ('project', 'Project'),
        ('won', 'Won'),
        ('lost', 'Lost'),
    ], default='new', required=True, tracking=True, index=True, copy=False)
    priority = fields.Selection([
        ('0', 'Normal'), ('1', 'Low'), ('2', 'High'), ('3', 'Very High'),
    ], default='0')
    followup_date = fields.Date(string='Follow-up On', tracking=True, index=True,
                                help="Reminder: the lead shows in 'My work today' from this date until it is won or lost.")
    followup_note = fields.Char(string='Follow-up Note')
    active = fields.Boolean(default=True)
    lost_reason = fields.Char(tracking=True, readonly=True, copy=False)
    lost_description = fields.Text(readonly=True, copy=False)
    lost_date = fields.Datetime(readonly=True, copy=False)
    lost_by_id = fields.Many2one('res.users', string='Lost By', readonly=True, copy=False)
    notes = fields.Html()
    attachment_ids = fields.Many2many(
        'ir.attachment', 'otm_lead_attachment_rel', 'lead_id', 'attachment_id',
        string='Attachments')

    otm_stage_tracker = fields.Json(string='Lifecycle Progress', compute='_compute_otm_stage_tracker')
    service_line_ids = fields.One2many('otm.lead.service.line', 'lead_id', string='Services')
    demo_ids = fields.One2many('otm.demo', 'lead_id', string='Demos')
    demo_count = fields.Integer(string='Demo Count', compute='_compute_counts')
    estimate_ids = fields.One2many('otm.estimate', 'lead_id', string='Estimates')
    estimate_count = fields.Integer(string='Estimate Count', compute='_compute_counts')
    deal_ids = fields.One2many('otm.deal', 'lead_id', string='Deals')
    deal_count = fields.Integer(string='Deal Count', compute='_compute_counts')
    service_count = fields.Integer(string='Service Lines', compute='_compute_service_count')
    base_total = fields.Monetary(
        string='Base Total', compute='_compute_base_total', store=True,
        currency_field='currency_id')

    # ------------------------------------------------------------------
    # Computes
    # ------------------------------------------------------------------
    @api.depends('demo_ids', 'estimate_ids', 'deal_ids')
    def _compute_counts(self):
        for lead in self:
            lead.demo_count = len(lead.demo_ids)
            lead.estimate_count = len(lead.estimate_ids)
            lead.deal_count = len(lead.sudo().deal_ids)

    @api.depends('service_line_ids')
    def _compute_otm_stage_tracker(self):
        Dash = self.env['otm.dashboard']
        for lead in self:
            lead.otm_stage_tracker = Dash.lifecycle_tracker(lead)

    def _compute_service_count(self):
        for lead in self:
            lead.service_count = len(lead.service_line_ids)

    @api.depends('service_line_ids.base_subtotal', 'currency_id')
    def _compute_base_total(self):
        for lead in self:
            total = sum(lead.service_line_ids.mapped('base_subtotal'))
            lead.base_total = lead.currency_id.round(total) if lead.currency_id else total

    # ------------------------------------------------------------------
    # Constraints / access checks
    # ------------------------------------------------------------------
    @api.constrains('sales_team_id', 'salesperson_id')
    def _check_salesperson_in_team(self):
        for lead in self:
            team = lead.sales_team_id.sudo()
            if lead.salesperson_id not in (team.head_id | team.member_ids):
                raise ValidationError(_(
                    "%(person)s is not a member of the sales team %(team)s.",
                    person=lead.salesperson_id.name, team=team.name))

    @api.constrains('stage', 'lost_reason')
    def _check_lost_reason(self):
        for lead in self:
            if lead.stage == 'lost' and not lead.lost_reason:
                raise ValidationError(_("Please enter a lost reason before marking the lead as lost."))

    def _otm_is_lifecycle_admin(self):
        return self.env.su or self.env.user.has_group('sales_project_lifecycle.group_lifecycle_admin')

    def _otm_is_sales_head_or_admin(self):
        return self._otm_is_lifecycle_admin() or self.env.user.has_group(
            'sales_project_lifecycle.group_sales_head')

    def _otm_check_team_access(self):
        """A non-admin may only place leads into a team they head or belong to."""
        if self._otm_is_lifecycle_admin():
            return
        user = self.env.user
        for lead in self:
            team = lead.sales_team_id.sudo()
            if team.head_id != user and user not in team.member_ids:
                raise AccessError(_("You can only use sales teams that you belong to."))

    # ------------------------------------------------------------------
    # Transition engine hooks
    # ------------------------------------------------------------------
    def _otm_log_scope(self):
        return (self.sales_team_id, self.salesperson_id)

    def _otm_check_actor(self, action):
        self.ensure_one()
        user = self.env.user
        if self.env.su or self._otm_is_lifecycle_admin():
            return
        if action == 'reopen':
            allowed = self.sales_head_id == user
        else:
            allowed = user in (self.sales_head_id | self.salesperson_id)
        if not allowed:
            raise AccessError(_(
                "You are not allowed to perform this action on lead %s. "
                "Only its salesperson, the Sales Head of its team or an Administrator can.",
                self.display_name))

    def _otm_prerequisites(self, action):
        missing = []
        if action == 'collect_requirement' and not (self.requirement_description or '').strip():
            missing.append(_("The customer requirement description is empty."))
        if action == 'estimate':
            if not self.sudo().demo_ids.filtered(lambda d: d.status == 'completed'):
                missing.append(_("At least one demo must be completed."))
            if not self.service_line_ids:
                missing.append(_("No services have been selected for this lead."))
        if action == 'system_project':
            deal = self.env['otm.deal'].sudo().search(
                [('lead_id', '=', self.id), ('status', '=', 'locked')], limit=1)
            if not deal:
                missing.append(_("There is no locked deal for this lead."))
            elif not deal.project_start_allowed:
                missing.append(_(deal.PROJECT_START_MESSAGE))
        return missing

    # ------------------------------------------------------------------
    # ORM overrides
    # ------------------------------------------------------------------
    @api.model_create_multi
    def create(self, vals_list):
        sequence = self.env['ir.sequence'].sudo()
        for vals in vals_list:
            if vals.get('stage', 'new') != 'new' and not self.env.su \
                    and not self.env.context.get('otm_transition'):
                raise UserError(_("A new lead must start in the 'New' stage."))
            if not vals.get('reference'):
                vals['reference'] = sequence.next_by_code('otm.lead') or _('New')
        leads = super().create(vals_list)
        leads._otm_check_team_access()
        return leads

    def write(self, vals):
        if not self.env.su:
            if {'salesperson_id', 'sales_team_id'} & vals.keys() and not self._otm_is_sales_head_or_admin():
                raise AccessError(_("Only a Sales Head or an Administrator can reassign a lead."))
            if 'stage' in vals and not self.env.context.get('otm_transition'):
                raise UserError(_(
                    "The stage cannot be edited directly. Use the workflow buttons."))
        res = super().write(vals)
        if {'salesperson_id', 'sales_team_id'} & vals.keys():
            self._otm_check_team_access()
        return res

    # ------------------------------------------------------------------
    # Controlled transitions (public business actions)
    # ------------------------------------------------------------------
    def action_contact(self):
        return self._otm_do_transition('contact')

    def action_collect_requirement(self):
        return self._otm_do_transition('collect_requirement')

    def action_demo(self):
        return self._otm_do_transition('demo')

    def action_estimate(self):
        return self._otm_do_transition('estimate')

    def action_negotiate(self):
        return self._otm_do_transition('negotiate')

    def action_open_lost_wizard(self):
        self.ensure_one()
        return {
            'type': 'ir.actions.act_window',
            'name': _('Mark Lead as Lost'),
            'res_model': 'otm.lead.lost.wizard',
            'view_mode': 'form',
            'target': 'new',
            'context': {'default_lead_id': self.id},
        }

    def action_mark_lost(self, reason=None, description=None):
        reason = (reason or '').strip()
        if not reason:
            raise UserError(_("A lost reason is required to mark a lead as lost."))
        required = self.env['ir.config_parameter'].sudo().get_param(
            'sales_project_lifecycle.lost_description_required')
        if required and not (description or '').strip():
            raise UserError(_("A lost description is required."))
        return self._otm_do_transition(
            'lose', reason=reason, extra_vals={
                'lost_reason': reason, 'lost_description': description or False,
                'lost_date': fields.Datetime.now(), 'lost_by_id': self.env.user.id})

    def action_reopen(self):
        return self._otm_do_transition('reopen', extra_vals={
            'lost_reason': False, 'lost_description': False,
            'lost_date': False, 'lost_by_id': False})

    def action_create_estimate(self):
        self.ensure_one()
        if self.stage not in ('estimate', 'negotiation'):
            raise UserError(_("Estimates can be created once the lead is in the Estimate stage."))
        if not self.service_line_ids:
            raise UserError(_("Select at least one service on the lead first."))
        estimate = self.env['otm.estimate'].create({
            'lead_id': self.id,
            'customer_id': self.customer_id.id,
            'line_ids': [(0, 0, {
                'service_id': line.service_id.id,
                'description': line.service_id.name,
                'quantity': line.quantity,
                'base_unit_price': line.base_unit_price,
            }) for line in self.service_line_ids],
        })
        return {
            'type': 'ir.actions.act_window', 'res_model': 'otm.estimate',
            'res_id': estimate.id, 'view_mode': 'form',
        }

    def action_open_demos(self):
        self.ensure_one()
        return {
            'type': 'ir.actions.act_window', 'name': _('Demos'), 'res_model': 'otm.demo',
            'view_mode': 'list,form', 'domain': [('lead_id', '=', self.id)],
            'context': {'default_lead_id': self.id},
        }

    def action_open_deals(self):
        self.ensure_one()
        return {
            'type': 'ir.actions.act_window', 'name': _('Deals'), 'res_model': 'otm.deal',
            'view_mode': 'list,form', 'domain': [('lead_id', '=', self.id)],
        }

    def action_open_estimates(self):
        self.ensure_one()
        return {
            'type': 'ir.actions.act_window', 'name': _('Estimates'), 'res_model': 'otm.estimate',
            'view_mode': 'list,form', 'domain': [('lead_id', '=', self.id)],
        }

    def action_open_history(self):
        self.ensure_one()
        return {
            'type': 'ir.actions.act_window',
            'name': _('Transition History'),
            'res_model': 'otm.transition.log',
            'view_mode': 'list,form',
            'domain': [('res_model', '=', self._name), ('res_id', '=', self.id)],
        }

    @api.model
    def _otm_find_or_create_partner(self, vals):
        """Reuse a contact with the same email or name, otherwise create it. Runs as superuser because sales
        users do not hold Odoo's 'Contact Creation' right; callers must have checked the user's sales role."""
        P = self.env['res.partner'].sudo()
        esc = lambda t: t.replace('\\', '\\\\').replace('%', '\\%').replace('_', '\\_')
        found = P
        if vals.get('email'):
            found = P.search([('email', '=ilike', esc(vals['email'].strip()))], limit=1)
        if not found:
            found = P.search([('name', '=ilike', esc(vals['name'].strip()))], limit=1)
        return found or P.create(vals)

    @api.model
    def otm_create_customer(self, name):
        """Used by the web app's 'Create customer' option; returns [id, display_name]."""
        name = (name or '').strip()
        if not name:
            raise UserError(_("Enter the customer name."))
        if not any(self.env.user.has_group(g) for g in (
                'sales_project_lifecycle.group_sales_executive', 'sales_project_lifecycle.group_sales_head',
                'sales_project_lifecycle.group_lifecycle_admin')):
            raise AccessError(_("Only sales users can create customers here."))
        partner = self._otm_find_or_create_partner({'name': name})
        return [partner.id, partner.display_name]

    def action_create_customer(self):
        self.ensure_one()
        if self.customer_id:
            raise UserError(_("A customer is already linked to this lead."))
        partner = self._otm_find_or_create_partner({
            'name': self.company_name or self.name,
            'is_company': bool(self.company_name),
            'email': self.email,
            'phone': self.contact_number,
            'city': self.location,
        })
        self.customer_id = partner
        return True
