from odoo import _, api, fields, models
from odoo.exceptions import AccessError, UserError

FIN_HEAD = 'sales_project_lifecycle.group_sales_head,sales_project_lifecycle.group_finance'
ALLOWED_DIRECT = {'notes', 'message_main_attachment_id'}

DEAL_MATRIX = {
    'lock': {'from': ('draft',), 'to': 'locked'},
    'revise': {'from': ('locked',), 'to': 'revision'},
    'relock': {'from': ('revision',), 'to': 'locked'},
    'cancel': {'from': ('locked', 'revision'), 'to': 'cancelled'},
}


class OtmDeal(models.Model):
    _name = 'otm.deal'
    _description = 'Locked Deal'
    _inherit = ['mail.thread', 'mail.activity.mixin', 'otm.transition.mixin']
    _order = 'id desc'
    _otm_state_field = 'status'
    _otm_matrix = DEAL_MATRIX
    _otm_reason_methods = ('action_revise', 'action_cancel')

    name = fields.Char(string='Deal', readonly=True, copy=False, default=lambda s: _('New'))
    lead_id = fields.Many2one('otm.lead', required=True, index=True, ondelete='restrict', tracking=True)
    estimate_id = fields.Many2one('otm.estimate', required=True, ondelete='restrict', tracking=True)
    estimate_revision = fields.Integer(readonly=True)
    customer_id = fields.Many2one('res.partner', readonly=True, index=True)
    sales_team_id = fields.Many2one('otm.sales.team', related='lead_id.sales_team_id', store=True, index=True)
    sales_head_id = fields.Many2one('res.users', related='lead_id.sales_head_id', store=True, index=True)
    salesperson_id = fields.Many2one('res.users', related='lead_id.salesperson_id', store=True, index=True)
    company_id = fields.Many2one('res.company', related='lead_id.company_id', store=True)
    currency_id = fields.Many2one('res.currency', related='company_id.currency_id')
    status = fields.Selection([
        ('draft', 'Draft'), ('locked', 'Locked'), ('revision', 'In Revision'),
        ('cancelled', 'Cancelled'),
    ], default='draft', required=True, tracking=True, index=True, copy=False)
    locked_by_id = fields.Many2one('res.users', readonly=True, copy=False)
    locked_date = fields.Datetime(readonly=True, copy=False)
    lock_reason = fields.Text(readonly=True, copy=False)
    revision_count = fields.Integer(readonly=True, copy=False)
    notes = fields.Text()

    line_ids = fields.One2many('otm.deal.line', 'deal_id', string='Services')
    commission_ids = fields.One2many('otm.sales.commission', 'deal_id', string='Commissions')
    commission_count = fields.Integer(compute='_compute_commission_count')
    # Snapshot of the approved estimate; frozen while the deal is locked
    base_total = fields.Monetary(currency_field='currency_id', readonly=True, groups=FIN_HEAD)
    additional_total = fields.Monetary(currency_field='currency_id', readonly=True, groups=FIN_HEAD)
    selling_total = fields.Monetary(currency_field='currency_id', readonly=True, groups=FIN_HEAD)
    customization_amount = fields.Monetary(currency_field='currency_id', readonly=True, groups=FIN_HEAD)
    discount_amount = fields.Monetary(currency_field='currency_id', readonly=True, groups=FIN_HEAD)
    tax_amount = fields.Monetary(currency_field='currency_id', readonly=True, groups=FIN_HEAD)
    total_amount = fields.Monetary(string='Deal Value', currency_field='currency_id', readonly=True, groups=FIN_HEAD)
    commission_total = fields.Monetary(
        currency_field='currency_id', readonly=True, groups=FIN_HEAD)

    agreement_ids = fields.One2many('otm.customer.agreement', 'deal_id', string='Agreements')
    project_id = fields.Many2one('project.project', string='Project', readonly=True, copy=False, index=True)
    payment_ids = fields.One2many('otm.deal.payment', 'deal_id', string='Payments')
    agreement_count = fields.Integer(compute='_compute_commission_count')
    amount_received = fields.Monetary(
        currency_field='currency_id', compute='_compute_payment_totals', store=True, groups=FIN_HEAD)
    balance_due = fields.Monetary(
        currency_field='currency_id', compute='_compute_payment_totals', store=True, groups=FIN_HEAD)
    project_start_allowed = fields.Boolean(
        compute='_compute_payment_totals', store=True,
        help="True when the agreement is completed and every advance installment is received.")
    project_start_message = fields.Char(compute='_compute_start_message')

    PROJECT_START_MESSAGE = (
        "Project cannot start until the customer agreement is completed and "
        "the required advance payment is received.")

    @api.depends('agreement_ids.status', 'payment_ids.status', 'payment_ids.trigger',
                 'payment_ids.amount', 'total_amount', 'status')
    def _compute_payment_totals(self):
        for deal in self:
            pays = deal.payment_ids.filtered(lambda p: p.status != 'cancelled')
            received = sum(pays.filtered(lambda p: p.status == 'received').mapped('amount'))
            deal.amount_received = received
            deal.balance_due = deal.total_amount - received
            advance = pays.filtered(lambda p: p.trigger == 'advance')
            deal.project_start_allowed = bool(
                deal.status in ('locked', 'revision')
                and deal.agreement_ids.filtered(lambda a: a.status == 'completed')
                and advance and all(p.status == 'received' for p in advance))

    def _compute_start_message(self):
        for deal in self:
            deal.project_start_message = False if deal.project_start_allowed else _(self.PROJECT_START_MESSAGE)

    def _otm_project_start_check(self):
        """Business-logic gate used by project creation / lead -> project transition."""
        for deal in self.sudo():
            if not deal.project_start_allowed:
                raise UserError(_(self.PROJECT_START_MESSAGE))
        return True

    def _otm_refresh_payment_totals(self):
        self.env.add_to_compute(self._fields['amount_received'], self)
        self.env.add_to_compute(self._fields['project_start_allowed'], self)
        self.env.add_to_compute(self._fields['balance_due'], self)

    def _otm_payment_event(self, trigger):
        """Make not-yet-due installments with this trigger due (called by lifecycle phases)."""
        for deal in self.sudo():
            deal.payment_ids.filtered(
                lambda p: p.status == 'pending' and p.trigger == trigger)._otm_do_transition('system_due')
        return True

    def _otm_after_advance_received(self):
        for deal in self:
            deal.message_post(body=_("Advance payment confirmed by Finance."))
            deal._otm_refresh_payment_totals()
            deal._otm_try_auto_project()

    def _otm_try_auto_project(self):
        """Optional setting: create the delivery project as soon as the agreement is completed AND
        the advance is received (whichever happens last). Never raises: a failure is noted on the deal."""
        ICP = self.env['ir.config_parameter'].sudo()
        if not ICP.get_param('sales_project_lifecycle.auto_create_project'):
            return False
        for deal in self.sudo():
            if deal.project_id or not deal.project_start_allowed:
                continue
            try:
                with self.env.cr.savepoint():
                    deal.action_create_project()
                deal.message_post(body=_("Project was created automatically (advance received, agreement completed)."))
            except Exception as exc:  # noqa: BLE001 - never block the payment / agreement transition
                deal.message_post(body=_("Automatic project creation failed: %s. Create it manually.", exc))
        return True

    @api.depends('commission_ids', 'agreement_ids')
    def _compute_commission_count(self):
        for deal in self:
            deal.commission_count = len(deal.sudo().commission_ids)
            deal.agreement_count = len(deal.sudo().agreement_ids)

    # -- access ---------------------------------------------------------
    @api.model_create_multi
    def create(self, vals_list):
        seq = self.env['ir.sequence'].sudo()
        for vals in vals_list:
            vals['name'] = seq.next_by_code('otm.deal') or _('New')
        return super().create(vals_list)

    def write(self, vals):
        if not (self.env.su or self.env.context.get('otm_transition')):
            if set(vals) - ALLOWED_DIRECT:
                raise UserError(_(
                    "The commercial terms of a deal are protected. Use 'Revise Deal' to change them."))
        return super().write(vals)

    def unlink(self):
        if not self.env.su:
            raise UserError(_("Deals cannot be deleted. Cancel the deal instead."))
        return super().unlink()

    # -- transition hooks -----------------------------------------------
    def _otm_log_scope(self):
        return (self.sales_team_id, self.sales_head_id)

    def _otm_check_actor(self, action):
        self.ensure_one()
        user = self.env.user
        if self.env.su or user.has_group('sales_project_lifecycle.group_lifecycle_admin'):
            return
        if user != self.sales_head_id:
            raise AccessError(_("Only the Sales Head of the team or an Administrator can do this."))

    def _otm_prerequisites(self, action):
        self.ensure_one()
        missing = []
        est = self.estimate_id.sudo()
        if action in ('lock', 'relock'):
            if est.status != 'approved':
                missing.append(_("The estimate must be approved by the customer first."))
            if est.lead_id != self.lead_id:
                missing.append(_("The estimate does not belong to this lead."))
            if self.lead_id.stage not in ('negotiation', 'deal_locked'):
                missing.append(_("The lead must be in the Negotiation stage."))
            if est.discount_amount and est.discount_approval_required and est.discount_state != 'approved':
                missing.append(_("The estimate discount is not approved."))
            if not est.customer_id:
                missing.append(_("The customer is not set."))
            for line in est.line_ids:
                svc = line.service_id
                if svc.commission_enabled and line.additional_amount \
                        and not self.env['otm.commission.rule']._resolve_for_service(svc):
                    missing.append(_("No commission rule is configured for %s.", svc.name))
        if action == 'lock':
            others = self.sudo().search([
                ('lead_id', '=', self.lead_id.id), ('status', 'in', ('locked', 'revision')),
                ('id', '!=', self.id)], limit=1)
            if others:
                missing.append(_("This lead already has an active deal (%s).", others.name))
        if action == 'relock' and est.revision_number <= self.estimate_revision:
            missing.append(_("The estimate has not been revised; there is nothing new to lock."))
        if action == 'revise' and self.sudo().agreement_ids.filtered(lambda a: a.status != 'cancelled'):
            missing.append(_("Cancel the customer agreement before revising the deal."))
        if action == 'cancel':
            if self.sudo().payment_ids.filtered(lambda p: p.status == 'received'):
                missing.append(_("A payment has already been received for this deal."))
            if self.sudo().agreement_ids.filtered(lambda a: a.status == 'completed'):
                missing.append(_("The customer agreement is already completed."))
        if action == 'revise':
            if self.sudo().commission_ids.filtered(lambda c: c.status not in ('pending', 'cancelled')):
                missing.append(_("A commission of this deal is already earned or paid; reverse it first."))
        if action == 'cancel':
            if self.sudo().commission_ids.filtered(lambda c: c.status in ('approved', 'paid')):
                missing.append(_("Approved or paid commissions must be reversed before cancelling the deal."))
        return missing

    def _otm_snapshot_vals(self):
        est = self.estimate_id.sudo()
        return {
            'estimate_revision': est.revision_number, 'customer_id': est.customer_id.id,
            'base_total': est.base_amount, 'additional_total': est.additional_amount,
            'selling_total': est.subtotal, 'customization_amount': est.customization_amount,
            'discount_amount': est.discount_amount, 'tax_amount': est.tax_amount,
            'total_amount': est.total_amount,
        }

    def _otm_rebuild_lines_and_commissions(self):
        """Copy the approved estimate into the frozen deal lines and (re)generate commissions."""
        Line = self.env['otm.deal.line'].sudo()
        Commission = self.env['otm.sales.commission'].sudo()
        Rule = self.env['otm.commission.rule']
        for deal in self:
            est = deal.estimate_id.sudo()
            deal.sudo().line_ids.unlink()
            deal.sudo().commission_ids.filtered(lambda c: c.status == 'pending')._otm_do_transition(
                'system_cancel', reason=_('Deal terms re-locked'))
            total = 0.0
            for eline in est.line_ids:
                dline = Line.create({
                    'deal_id': deal.id, 'service_id': eline.service_id.id,
                    'description': eline.description, 'quantity': eline.quantity,
                    'base_unit_price': eline.base_unit_price,
                    'additional_amount': eline.additional_amount})
                svc = eline.service_id
                if not (svc.commission_enabled and dline.additional_subtotal):
                    continue
                rule = Rule._resolve_for_service(svc)
                amount = rule._compute_commission(dline.additional_subtotal)
                total += amount
                Commission.create({
                    'deal_id': deal.id, 'deal_line_id': dline.id, 'rule_id': rule.id,
                    'sales_head_id': deal.sales_head_id.id, 'sales_team_id': deal.sales_team_id.id,
                    'lead_id': deal.lead_id.id, 'estimate_id': est.id,
                    'customer_id': deal.customer_id.id, 'service_id': svc.id,
                    'base_amount': dline.base_subtotal,
                    'additional_amount': dline.additional_subtotal,
                    'commission_amount': amount,
                    'trigger': rule.trigger, 'approval_required': rule.approval_required})
            deal.with_context(otm_transition=True).write({'commission_total': total})

    def _otm_after_transition(self, action, old_state, reason):
        for deal in self:
            if action in ('lock', 'relock'):
                vals = deal._otm_snapshot_vals()
                vals.update({'locked_by_id': self.env.user.id, 'locked_date': fields.Datetime.now(),
                             'lock_reason': reason or deal.lock_reason})
                deal.with_context(otm_transition=True).write(vals)
                deal._otm_rebuild_lines_and_commissions()
                if action == 'lock':
                    deal.lead_id.sudo()._otm_do_transition('system_deal_lock')
            elif action == 'revise':
                deal.with_context(otm_transition=True).write({'revision_count': deal.revision_count + 1})
            elif action == 'cancel':
                deal.sudo().agreement_ids.filtered(
                    lambda a: a.status not in ('cancelled', 'completed')
                )._otm_do_transition('system_cancel', reason=_('Deal cancelled'))
                deal.sudo().commission_ids.filtered(
                    lambda c: c.status in ('pending', 'earned'))._otm_do_transition(
                    'system_cancel', reason=_('Deal cancelled'))
                if deal.lead_id.sudo().stage in ('deal_locked', 'agreement', 'advance_pending'):
                    deal.lead_id.sudo()._otm_do_transition('system_deal_unlock')

    # -- public actions -------------------------------------------------
    @api.model
    def _otm_lock_estimate(self, estimate, reason):
        if not (reason or '').strip():
            raise UserError(_("A lock reason is required."))
        deal = self.create({
            'lead_id': estimate.lead_id.id, 'estimate_id': estimate.id,
            'customer_id': estimate.customer_id.id})
        deal._otm_do_transition('lock', reason=reason, extra_vals={'lock_reason': reason})
        return deal

    def action_revise(self, reason=None):
        self._otm_require_reason(reason)
        return self._otm_do_transition('revise', reason=reason)

    def action_relock(self, reason=None):
        self._otm_require_reason(reason)
        return self._otm_do_transition('relock', reason=reason, extra_vals={'lock_reason': reason})

    def action_cancel(self, reason=None):
        self._otm_require_reason(reason)
        return self._otm_do_transition('cancel', reason=reason)

    def _otm_require_reason(self, reason):
        if not (reason or '').strip():
            raise UserError(_("A reason is required for this action."))

    def _otm_commission_event(self, event):
        """Called by later phases (payments, project) when a commission trigger happens."""
        for deal in self.sudo():
            comms = deal.commission_ids.filtered(
                lambda c: c.status == 'pending' and c.trigger == event)
            comms._otm_do_transition('system_earn', reason=event)
            comms.filtered(lambda c: not c.approval_required)._otm_do_transition(
                'system_approve', reason=_('No approval required'))
        return True

    def action_create_agreement(self):
        self.ensure_one()
        user = self.env.user
        if not (self.env.su or user.has_group('sales_project_lifecycle.group_lifecycle_admin')
                or user in (self.sales_head_id | self.salesperson_id)):
            raise AccessError(_("Only the salesperson or the Sales Head can create the agreement."))
        if self.status != 'locked':
            raise UserError(_("Agreements can only be created for a locked deal."))
        if self.sudo().agreement_ids.filtered(lambda a: a.status != 'cancelled'):
            raise UserError(_("This deal already has an active agreement."))
        services = '\n'.join(f"- {l.description or l.service_id.name}" for l in self.sudo().line_ids)
        sched = self.env['otm.payment.schedule']._get_default()
        days = sum(self.sudo().line_ids.mapped('service_id.default_duration'))
        ag = self.env['otm.customer.agreement'].create({
            'deal_id': self.id,
            'scope': (self.lead_id.requirement_description or '') or services,
            'deliverables': services,
            'payment_schedule_id': sched.id, 'timeline_days': days})
        return {'type': 'ir.actions.act_window', 'res_model': 'otm.customer.agreement',
                'res_id': ag.id, 'view_mode': 'form'}

    def action_create_project(self):
        """Create the delivery project once the agreement is completed and the advance received."""
        self.ensure_one()
        user = self.env.user
        if not (self.env.su or user.has_group('sales_project_lifecycle.group_lifecycle_admin')
                or user == self.sales_head_id):
            raise AccessError(_("Only the Sales Head of the team or an Administrator can create the project."))
        if self.sudo().project_id:
            raise UserError(_("A project already exists for this deal."))
        self._otm_project_start_check()
        deal = self.sudo()
        ag = deal.agreement_ids.filtered(lambda a: a.status == 'completed')[:1]
        lead = deal.lead_id
        head = self.env['res.users']  # a Project Head takes the project over and assigns themselves
        project = self.env['project.project'].sudo().with_context(mail_create_nosubscribe=True).create({
            'name': f"{lead.name} - {deal.customer_id.name}",
            'partner_id': deal.customer_id.id,
            'user_id': head.id,
            'privacy_visibility': 'followers',
            'otm_is_lifecycle': True, 'otm_state': 'planning',
            'otm_deal_id': deal.id, 'otm_lead_id': lead.id, 'otm_agreement_id': ag.id,
            'otm_estimate_id': deal.estimate_id.id,
            'otm_sales_team_id': deal.sales_team_id.id,
            'otm_sales_head_id': deal.sales_head_id.id,
            'otm_salesperson_id': deal.salesperson_id.id,
            'otm_scope': ag.scope, 'otm_deliverables': ag.deliverables,
            'otm_requirements': lead.requirement_description,
            'otm_services': ag.approved_services,
            'otm_customizations': ag.approved_customizations,
        })
        project._otm_create_stage_lines()
        project.message_subscribe(partner_ids=(
            deal.sales_head_id | deal.salesperson_id | head).partner_id.ids)
        Att = self.env['ir.attachment'].sudo()
        for att in Att.search([('res_model', '=', 'otm.lead'), ('res_id', '=', lead.id)]) \
                | Att.search([('res_model', '=', 'res.partner'), ('res_id', '=', deal.customer_id.id)]):
            att.copy({'res_model': 'project.project', 'res_id': project.id})
        deal.with_context(otm_transition=True).write({'project_id': project.id})
        ag.with_context(otm_transition=True).write({'project_id': project.id})
        if lead.stage == 'advance_pending':
            lead._otm_do_transition('system_project')
        deal.message_post(body=_("Project %s created.", project.name))
        return {'type': 'ir.actions.act_window', 'res_model': 'project.project',
                'res_id': project.id, 'view_mode': 'form',
                'views': [(self.env.ref('sales_project_lifecycle.view_otm_project_form').id, 'form')]}

    def action_open_project(self):
        self.ensure_one()
        return {'type': 'ir.actions.act_window', 'res_model': 'project.project',
                'res_id': self.sudo().project_id.id, 'view_mode': 'form',
                'views': [(self.env.ref('sales_project_lifecycle.view_otm_project_form').id, 'form')]}

    def action_open_agreements(self):
        self.ensure_one()
        return {'type': 'ir.actions.act_window', 'name': _('Agreements'),
                'res_model': 'otm.customer.agreement', 'view_mode': 'list,form',
                'domain': [('deal_id', '=', self.id)]}

    def action_open_payments(self):
        self.ensure_one()
        return {'type': 'ir.actions.act_window', 'name': _('Payments'),
                'res_model': 'otm.deal.payment', 'view_mode': 'list,form',
                'domain': [('deal_id', '=', self.id)]}

    def action_open_commissions(self):
        self.ensure_one()
        return {
            'type': 'ir.actions.act_window', 'name': _('Commissions'),
            'res_model': 'otm.sales.commission', 'view_mode': 'list,form',
            'domain': [('deal_id', '=', self.id)],
        }

    def action_open_history(self):
        self.ensure_one()
        return {
            'type': 'ir.actions.act_window', 'name': _('History'),
            'res_model': 'otm.transition.log', 'view_mode': 'list,form',
            'domain': [('res_model', '=', self._name), ('res_id', '=', self.id)],
        }


class OtmDealLine(models.Model):
    _name = 'otm.deal.line'
    _description = 'Locked Deal Line'
    _order = 'deal_id, id'

    deal_id = fields.Many2one('otm.deal', required=True, ondelete='cascade', index=True)
    currency_id = fields.Many2one('res.currency', related='deal_id.currency_id')
    service_id = fields.Many2one('otm.service', required=True)
    description = fields.Char()
    quantity = fields.Float(digits='Product Unit')
    base_unit_price = fields.Monetary(currency_field='currency_id', groups=FIN_HEAD)
    additional_amount = fields.Monetary(
        string='Additional (per unit)', currency_field='currency_id', groups=FIN_HEAD)
    additional_percentage = fields.Float(
        compute='_compute_amounts', store=True, digits=(16, 2), groups=FIN_HEAD)
    selling_unit_price = fields.Monetary(currency_field='currency_id', compute='_compute_amounts', store=True, groups=FIN_HEAD)
    base_subtotal = fields.Monetary(currency_field='currency_id', compute='_compute_amounts', store=True, groups=FIN_HEAD)
    additional_subtotal = fields.Monetary(
        currency_field='currency_id', compute='_compute_amounts', store=True, groups=FIN_HEAD)
    selling_subtotal = fields.Monetary(currency_field='currency_id', compute='_compute_amounts', store=True, groups=FIN_HEAD)

    @api.depends('quantity', 'base_unit_price', 'additional_amount')
    def _compute_amounts(self):
        for line in self:
            rnd = line.currency_id.round if line.currency_id else (lambda x: x)
            line.selling_unit_price = line.base_unit_price + line.additional_amount
            line.base_subtotal = rnd(line.quantity * line.base_unit_price)
            line.additional_subtotal = rnd(line.quantity * line.additional_amount)
            line.selling_subtotal = rnd(line.quantity * line.selling_unit_price)
            line.additional_percentage = (
                line.additional_amount / line.base_unit_price * 100.0) if line.base_unit_price else 0.0

    def write(self, vals):
        if not self.env.su:
            raise UserError(_("Deal lines are protected. Use 'Revise Deal' to change them."))
        return super().write(vals)

    def unlink(self):
        if not self.env.su:
            raise UserError(_("Deal lines are protected. Use 'Revise Deal' to change them."))
        return super().unlink()
