from datetime import timedelta

from odoo import _, api, fields, models
from odoo.exceptions import AccessError, UserError, ValidationError

EDITABLE = ('draft', 'internal_review')

EST_MATRIX = {
    'submit': {'from': ('draft',), 'to': 'internal_review'},
    'reset_draft': {'from': ('internal_review',), 'to': 'draft'},
    'send': {'from': ('internal_review',), 'to': 'sent'},
    'negotiate': {'from': ('sent',), 'to': 'negotiation'},
    'approve': {'from': ('sent', 'negotiation'), 'to': 'approved'},
    'reject': {'from': ('internal_review', 'sent', 'negotiation'), 'to': 'rejected'},
    'expire': {'from': ('sent', 'negotiation'), 'to': 'expired', 'system': True},
    'revise': {'from': ('approved', 'sent', 'negotiation', 'rejected', 'expired'), 'to': 'draft'},
    'request_discount': {
        'field': 'discount_state', 'from': ('not_requested', 'rejected'), 'to': 'pending'},
    'approve_discount': {'field': 'discount_state', 'from': ('pending',), 'to': 'approved'},
    'reject_discount': {'field': 'discount_state', 'from': ('pending',), 'to': 'rejected'},
}
# Fields that may only change inside the controlled workflow
PROTECTED_FIELDS = {
    'status', 'discount_state', 'revision_number', 'discount_approved_by_id',
    'discount_approved_date', 'revision_start_amount', 'revision_reason',
}
# Commercial fields frozen once the estimate has left draft / internal review
FROZEN_FIELDS = {'discount_type', 'discount_value', 'discount_reason', 'tax_percent', 'lead_id'}

LEVEL_NAMES = {1: 'a Sales Head', 2: 'an Administrator'}


class OtmEstimate(models.Model):
    _name = 'otm.estimate'
    _description = 'Estimate'
    _inherit = ['mail.thread', 'mail.activity.mixin', 'otm.transition.mixin']
    _order = 'id desc'
    _otm_state_field = 'status'
    _otm_matrix = EST_MATRIX
    _otm_email_map = {'send': 'sales_project_lifecycle.mail_estimate'}

    def _otm_email_partner(self):
        return self.customer_id

    def _otm_template_xmlid(self, action):
        if action == 'send':
            return 'sales_project_lifecycle.mail_estimate_revision' if self.revision_number > 1 else 'sales_project_lifecycle.mail_estimate'
        return None
    _otm_reason_methods = (
        'action_reject', 'action_revise', 'action_reject_discount', 'action_reset_draft',
        'action_lock_deal')

    estimate_number = fields.Char(readonly=True, copy=False, index=True)
    revision_number = fields.Integer(default=1, readonly=True, copy=False, tracking=True)
    lead_id = fields.Many2one(
        'otm.lead', string='Lead', required=True, index=True, ondelete='restrict')
    customer_id = fields.Many2one(
        'res.partner', string='Customer', tracking=True, index=True,
        compute='_compute_customer', store=True, readonly=False, precompute=True)
    sales_team_id = fields.Many2one(
        'otm.sales.team', related='lead_id.sales_team_id', store=True, index=True)
    sales_head_id = fields.Many2one(
        'res.users', related='lead_id.sales_head_id', store=True, index=True, string='Sales Head')
    salesperson_id = fields.Many2one(
        'res.users', related='lead_id.salesperson_id', store=True, index=True, string='Salesperson')
    company_id = fields.Many2one('res.company', related='lead_id.company_id', store=True)
    currency_id = fields.Many2one('res.currency', related='company_id.currency_id', string='Currency')
    estimate_date = fields.Date(default=fields.Date.context_today, required=True)
    validity_date = fields.Date(
        string='Valid Until', required=True, tracking=True,
        default=lambda self: fields.Date.context_today(self) + timedelta(days=30))
    status = fields.Selection([
        ('draft', 'Draft'),
        ('internal_review', 'Internal Review'),
        ('sent', 'Sent'),
        ('negotiation', 'Negotiation'),
        ('approved', 'Approved'),
        ('rejected', 'Rejected'),
        ('expired', 'Expired'),
    ], default='draft', required=True, tracking=True, index=True, copy=False)
    notes = fields.Html()

    line_ids = fields.One2many('otm.estimate.line', 'estimate_id', string='Services', copy=True)
    customization_ids = fields.One2many(
        'otm.estimate.customization', 'estimate_id', string='Customizations', copy=True)
    revision_ids = fields.One2many('otm.estimate.revision', 'estimate_id', string='Revisions')
    revision_start_amount = fields.Monetary(
        currency_field='currency_id', readonly=True, copy=False)
    revision_reason = fields.Text(readonly=True, copy=False)

    # --- amounts (all system calculated) ---------------------------------
    base_amount = fields.Monetary(
        currency_field='currency_id', compute='_compute_line_totals', store=True)
    additional_amount = fields.Monetary(
        currency_field='currency_id', compute='_compute_line_totals', store=True,
        groups='sales_project_lifecycle.group_sales_head')
    subtotal = fields.Monetary(
        string='Selling Total', currency_field='currency_id',
        compute='_compute_line_totals', store=True,
        help="Customer selling amount: base amount + additional selling amount.")
    customization_amount = fields.Monetary(
        currency_field='currency_id', compute='_compute_customization_amount', store=True)

    discount_type = fields.Selection(
        [('percentage', 'Percentage'), ('fixed', 'Fixed Amount')], default='percentage', required=True)
    discount_value = fields.Float(digits=(16, 2))
    discount_amount = fields.Monetary(
        currency_field='currency_id', compute='_compute_discount_amount', store=True)
    discount_percent = fields.Float(
        string='Effective Discount %', digits=(16, 2), compute='_compute_discount_amount', store=True)
    discount_reason = fields.Text()
    discount_state = fields.Selection([
        ('not_requested', 'Not Requested'),
        ('pending', 'Pending Approval'),
        ('approved', 'Approved'),
        ('rejected', 'Rejected'),
    ], default='not_requested', required=True, tracking=True, copy=False)
    discount_user_id = fields.Many2one(
        'res.users', string='Discount Entered By', readonly=True, copy=False,
        default=lambda self: self.env.user)
    discount_required_level = fields.Selection(
        [('none', 'No approval'), ('head', 'Sales Head'), ('admin', 'Administrator')],
        compute='_compute_discount_approval', string='Approval Level Needed')
    discount_approval_required = fields.Boolean(compute='_compute_discount_approval')
    discount_approved_by_id = fields.Many2one('res.users', string='Approved By', readonly=True, copy=False)
    discount_approved_date = fields.Datetime(string='Approved On', readonly=True, copy=False)

    tax_percent = fields.Float(
        string='Tax %', digits=(16, 2),
        default=lambda self: float(self.env['ir.config_parameter'].sudo().get_param(
            'sales_project_lifecycle.default_tax_percent', 0.0)))
    tax_amount = fields.Monetary(
        currency_field='currency_id', compute='_compute_totals', store=True)
    total_amount = fields.Monetary(
        string='Final Estimate', currency_field='currency_id', compute='_compute_totals',
        store=True, tracking=True)

    # ------------------------------------------------------------------
    # Computes
    # ------------------------------------------------------------------
    @api.depends('lead_id')
    def _compute_customer(self):
        for est in self:
            if not est.customer_id:
                est.customer_id = est.lead_id.sudo().customer_id

    @api.depends('line_ids.base_subtotal', 'line_ids.additional_subtotal',
                 'line_ids.subtotal', 'currency_id')
    def _compute_line_totals(self):
        for est in self:
            est.base_amount = sum(est.line_ids.mapped('base_subtotal'))
            est.additional_amount = sum(est.line_ids.mapped('additional_subtotal'))
            est.subtotal = sum(est.line_ids.mapped('subtotal'))

    @api.depends('customization_ids.calculated_amount')
    def _compute_customization_amount(self):
        for est in self:
            est.customization_amount = sum(est.customization_ids.mapped('calculated_amount'))

    @api.depends('subtotal', 'customization_amount', 'discount_type', 'discount_value', 'currency_id')
    def _compute_discount_amount(self):
        for est in self:
            base = est.subtotal + est.customization_amount
            if est.discount_type == 'percentage':
                amount = base * est.discount_value / 100.0
            else:
                amount = est.discount_value
            amount = min(max(amount, 0.0), base)
            est.discount_amount = est.currency_id.round(amount) if est.currency_id else amount
            est.discount_percent = (est.discount_amount / base * 100.0) if base else 0.0

    @api.depends('subtotal', 'customization_amount', 'discount_amount', 'tax_percent', 'currency_id')
    def _compute_totals(self):
        for est in self:
            taxable = est.subtotal + est.customization_amount - est.discount_amount
            tax = taxable * est.tax_percent / 100.0
            est.tax_amount = est.currency_id.round(tax) if est.currency_id else tax
            est.total_amount = taxable + est.tax_amount

    def _otm_discount_limits(self):
        param = self.env['ir.config_parameter'].sudo().get_param
        return (float(param('sales_project_lifecycle.executive_discount_limit', 5.0)),
                float(param('sales_project_lifecycle.head_discount_limit', 15.0)))

    def _otm_user_level(self, user):
        """2 = administrator, 1 = Sales Head of the team, 0 = salesperson, -1 = none."""
        self.ensure_one()
        if user.has_group('sales_project_lifecycle.group_lifecycle_admin'):
            return 2
        if user == self.sales_head_id:
            return 1
        if user == self.salesperson_id:
            return 0
        return -1

    @api.depends('discount_percent', 'discount_amount', 'discount_user_id', 'sales_head_id')
    def _compute_discount_approval(self):
        exec_limit, head_limit = self._otm_discount_limits()
        for est in self:
            pct = est.discount_percent if est.discount_amount else 0.0
            if pct <= exec_limit:
                needed, level = 'none', 0
            elif pct <= head_limit:
                needed, level = 'head', 1
            else:
                needed, level = 'admin', 2
            est.discount_required_level = needed
            authority = est._otm_user_level(est.discount_user_id) if est.discount_user_id else 0
            est.discount_approval_required = bool(est.discount_amount) and level > max(authority, 0)

    # ------------------------------------------------------------------
    # Constraints
    # ------------------------------------------------------------------
    @api.constrains('discount_type', 'discount_value')
    def _check_discount(self):
        for est in self:
            if est.discount_value < 0:
                raise ValidationError(_("The discount cannot be negative."))
            if est.discount_type == 'percentage' and est.discount_value > 100:
                raise ValidationError(_("A percentage discount cannot exceed 100%."))
            if est.discount_type == 'fixed' and est.discount_value > est.subtotal + est.customization_amount:
                raise ValidationError(_("The discount cannot exceed the estimate amount."))

    @api.constrains('tax_percent')
    def _check_tax(self):
        for est in self:
            if not 0 <= est.tax_percent <= 100:
                raise ValidationError(_("The tax percentage must be between 0 and 100."))

    @api.constrains('estimate_date', 'validity_date')
    def _check_dates(self):
        for est in self:
            if est.validity_date < est.estimate_date:
                raise ValidationError(_("The validity date cannot be before the estimate date."))

    # ------------------------------------------------------------------
    # Display
    # ------------------------------------------------------------------
    @api.depends('estimate_number', 'revision_number')
    def _compute_display_name(self):
        for est in self:
            number = est.estimate_number or _('New')
            est.display_name = f"{number}-V{est.revision_number}" if est.revision_number > 1 else number

    # ------------------------------------------------------------------
    # ORM
    # ------------------------------------------------------------------
    @api.model_create_multi
    def create(self, vals_list):
        sequence = self.env['ir.sequence'].sudo()
        for vals in vals_list:
            if (PROTECTED_FIELDS & vals.keys()) and not self.env.su \
                    and not self.env.context.get('otm_transition'):
                raise UserError(_("Workflow fields cannot be set directly."))
            vals['estimate_number'] = sequence.next_by_code('otm.estimate') or _('New')
            vals['discount_user_id'] = self.env.user.id
        return super().create(vals_list)

    def write(self, vals):
        in_workflow = self.env.su or self.env.context.get('otm_transition')
        if not in_workflow:
            if PROTECTED_FIELDS & vals.keys():
                raise UserError(_(
                    "Workflow fields cannot be edited directly. Use the workflow buttons."))
            if FROZEN_FIELDS & vals.keys():
                for est in self:
                    if est.status not in EDITABLE:
                        raise UserError(_(
                            "Estimate %s is locked. Use 'Revise' to change its commercial terms.",
                            est.display_name))
            if {'discount_type', 'discount_value'} & vals.keys():
                # a changed discount invalidates any earlier request / approval
                vals = dict(vals, discount_user_id=self.env.user.id)
                res = super().write(vals)
                self.with_context(otm_transition=True).write({
                    'discount_state': 'not_requested',
                    'discount_approved_by_id': False, 'discount_approved_date': False})
                return res
        return super().write(vals)

    # ------------------------------------------------------------------
    # Transition engine hooks
    # ------------------------------------------------------------------
    def _otm_log_scope(self):
        return (self.sales_team_id, self.salesperson_id)

    def _otm_check_actor(self, action):
        self.ensure_one()
        if self.env.su:
            return
        user = self.env.user
        level = self._otm_user_level(user)
        if level < 0:
            raise AccessError(_(
                "Only the salesperson, the Sales Head of the team or an Administrator can do this."))
        if action in ('send', 'approve', 'reset_draft') and level < 1:
            raise AccessError(_("Only a Sales Head or an Administrator can do this."))
        if action in ('approve_discount', 'reject_discount'):
            needed = {'none': 0, 'head': 1, 'admin': 2}[self.discount_required_level]
            if level < max(needed, 1):
                raise AccessError(_(
                    "This discount (%(pct).2f%%) must be approved by %(who)s.",
                    pct=self.discount_percent, who=LEVEL_NAMES[max(needed, 1)]))
            requester = self.discount_user_id
            if level < 2 and requester == user:
                raise AccessError(_("You cannot approve a discount that you entered yourself."))

    def _otm_commercial_blockers(self):
        self.ensure_one()
        missing = []
        if not self.customer_id:
            missing.append(_("The customer is not set."))
        if not self.line_ids:
            missing.append(_("The estimate has no services."))
        for line in self.line_ids:
            if line.quantity <= 0:
                missing.append(_("%s has an invalid quantity.", line.service_id.display_name))
            if line.selling_unit_price < 0 or line.base_unit_price < 0:
                missing.append(_("%s has an invalid amount.", line.service_id.display_name))
        if self.total_amount <= 0:
            missing.append(_("The final estimate amount must be greater than zero."))
        if self.discount_amount and self.discount_approval_required \
                and self.discount_state != 'approved':
            missing.append(_(
                "The discount of %(pct).2f%% needs approval from %(who)s (status: %(state)s).",
                pct=self.discount_percent,
                who=LEVEL_NAMES.get({'head': 1, 'admin': 2}.get(self.discount_required_level, 1)),
                state=self._otm_state_label(self.discount_state, 'discount_state')))
        return missing

    def _otm_prerequisites(self, action):
        if action == 'revise':
            deal = self.env['otm.deal'].sudo().search(
                [('estimate_id', '=', self.id), ('status', '=', 'locked')], limit=1)
            if deal:
                return [_("Deal %s is locked. The Sales Head must start a deal revision first.", deal.name)]
            return []
        if action in ('submit', 'send', 'approve'):
            return self._otm_commercial_blockers()
        if action == 'request_discount':
            missing = []
            if not self.discount_amount:
                missing.append(_("There is no discount to approve."))
            elif not self.discount_approval_required:
                missing.append(_("This discount is within your authority; no approval is needed."))
            if not (self.discount_reason or '').strip():
                missing.append(_("A discount reason is required."))
            return missing
        return []

    def _otm_after_transition(self, action, old_state, reason):
        if action == 'send':
            self._otm_sync_revision()
        if action == 'approve_discount':
            self.with_context(otm_transition=True).write({
                'discount_approved_by_id': self.env.user.id,
                'discount_approved_date': fields.Datetime.now()})

    def _otm_sync_revision(self):
        Revision = self.env['otm.estimate.revision'].sudo()
        for est in self:
            vals = {
                'previous_amount': est.revision_start_amount,
                'new_amount': est.total_amount,
                'reason': est.revision_reason or _('Initial estimate'),
                'changed_by_id': self.env.user.id,
                'changed_date': fields.Datetime.now(),
            }
            existing = Revision.search([
                ('estimate_id', '=', est.id), ('revision_number', '=', est.revision_number)], limit=1)
            if existing:
                existing.write(vals)
            else:
                Revision.create(dict(vals, estimate_id=est.id, revision_number=est.revision_number))

    # ------------------------------------------------------------------
    # Public business actions
    # ------------------------------------------------------------------
    def action_recalculate(self):
        for est in self:
            if est.status not in EDITABLE:
                raise UserError(_("Estimate %s is locked. Use 'Revise' first.", est.display_name))
            for fname in ('base_amount', 'additional_amount', 'subtotal', 'customization_amount',
                          'discount_amount', 'discount_percent', 'tax_amount', 'total_amount'):
                self.env.add_to_compute(self._fields[fname], est)
            est.message_post(body=_(
                "Estimate recalculated. Final amount: %(amount)s", amount=est.currency_id.format(
                    est.sudo().total_amount)))
        return True

    def action_submit(self):
        return self._otm_do_transition('submit')

    def action_reset_draft(self, reason=None):
        self._otm_require_reason(reason)
        return self._otm_do_transition('reset_draft', reason=reason)

    def action_send(self):
        return self._otm_do_transition('send')

    def action_negotiate(self):
        return self._otm_do_transition('negotiate')

    def action_approve(self):
        return self._otm_do_transition('approve')

    def action_reject(self, reason=None):
        self._otm_require_reason(reason)
        return self._otm_do_transition('reject', reason=reason)

    def action_revise(self, reason=None):
        self._otm_require_reason(reason)
        for est in self:
            est._otm_do_transition('revise', reason=reason, extra_vals={
                'revision_number': est.revision_number + 1,
                'revision_start_amount': est.total_amount,
                'revision_reason': reason})
        return True

    def action_lock_deal(self, reason=None):
        self.ensure_one()
        if self._otm_user_level(self.env.user) < 1 and not self.env.su:
            raise AccessError(_("Only the Sales Head of the team or an Administrator can lock a deal."))
        deal = self.env['otm.deal']._otm_lock_estimate(self, reason)
        return {'type': 'ir.actions.act_window', 'res_model': 'otm.deal',
                'res_id': deal.id, 'view_mode': 'form'}

    def action_request_discount(self):
        return self._otm_do_transition('request_discount')

    def action_approve_discount(self):
        return self._otm_do_transition('approve_discount')

    def action_reject_discount(self, reason=None):
        self._otm_require_reason(reason)
        return self._otm_do_transition('reject_discount', reason=reason)

    def _otm_require_reason(self, reason):
        if not (reason or '').strip():
            raise UserError(_("A reason is required for this action."))

    @api.model
    def _cron_expire_estimates(self):
        today = fields.Date.context_today(self)
        due = self.search([
            ('status', 'in', ('sent', 'negotiation')), ('validity_date', '<', today)])
        for est in due:
            est._otm_do_transition('expire', reason=_('Validity date passed'))


class OtmEstimateRevision(models.Model):
    """One row per estimate version (V1, V2, ...). Append-only history."""
    _name = 'otm.estimate.revision'
    _description = 'Estimate Revision'
    _order = 'estimate_id, revision_number'

    estimate_id = fields.Many2one(
        'otm.estimate', required=True, ondelete='cascade', index=True)
    sales_team_id = fields.Many2one(
        'otm.sales.team', related='estimate_id.sales_team_id', store=True, index=True)
    salesperson_id = fields.Many2one(
        'res.users', related='estimate_id.salesperson_id', store=True, index=True)
    currency_id = fields.Many2one('res.currency', related='estimate_id.currency_id')
    revision_number = fields.Integer(required=True)
    previous_amount = fields.Monetary(currency_field='currency_id')
    new_amount = fields.Monetary(currency_field='currency_id')
    reason = fields.Text()
    changed_by_id = fields.Many2one('res.users', string='Changed By')
    changed_date = fields.Datetime(string='Changed On')

    _estimate_revision_uniq = models.Constraint(
        'unique(estimate_id, revision_number)', 'A revision number can only be used once per estimate.')

    def write(self, vals):
        if not self.env.su:
            raise UserError(_("Estimate revisions cannot be modified."))
        return super().write(vals)

    def unlink(self):
        if not self.env.su:
            raise UserError(_("Estimate revisions cannot be deleted."))
        return super().unlink()
