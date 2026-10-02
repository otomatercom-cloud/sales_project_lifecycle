from datetime import timedelta

from odoo import _, api, fields, models
from odoo.exceptions import AccessError, UserError, ValidationError

SALES_GROUPS = ('sales_project_lifecycle.group_sales_executive,'
                'sales_project_lifecycle.group_finance')
ADMIN = 'sales_project_lifecycle.group_lifecycle_admin'
PROJECT_HEAD = 'sales_project_lifecycle.group_project_head'

PROJECT_MATRIX = {
    'start': {'from': ('planning',), 'to': 'in_progress'},
    'hold': {'from': ('in_progress',), 'to': 'on_hold'},
    'resume': {'from': ('on_hold',), 'to': 'in_progress'},
    'cancel': {'from': ('planning', 'in_progress', 'on_hold'), 'to': 'cancelled'},
    'deliver': {'from': ('in_progress',), 'to': 'delivered'},
    'close': {'from': ('delivered',), 'to': 'closed'},
}
# Snapshot of the approved commercial scope: only the system may change these
PROTECTED = {
    'otm_is_lifecycle', 'otm_state', 'otm_deal_id', 'otm_lead_id', 'otm_agreement_id',
    'otm_estimate_id', 'otm_sales_team_id', 'otm_sales_head_id', 'otm_salesperson_id',
    'otm_delivered_date', 'otm_delivered_by_id', 'otm_closed_date', 'otm_closed_by_id',
    'otm_scope', 'otm_deliverables', 'otm_requirements', 'otm_services', 'otm_customizations',
}
TEAM_FIELDS = {'user_id', 'otm_developer_ids', 'otm_qc_user_id', 'otm_deploy_user_id',
               'otm_trainer_id', 'otm_start_date', 'otm_technical_requirements', 'otm_stage_ids',
               'privacy_visibility'}


class ProjectProject(models.Model):
    _name = 'project.project'
    _inherit = ['project.project', 'otm.transition.mixin']
    _otm_state_field = 'otm_state'
    _otm_matrix = PROJECT_MATRIX
    _otm_email_map = {'start': 'sales_project_lifecycle.mail_project_confirmation'}

    def _otm_email_partner(self):
        return self.partner_id
    _otm_reason_methods = ('action_hold', 'action_cancel')

    otm_is_lifecycle = fields.Boolean(string='Sales Lifecycle Project', readonly=True, copy=False)
    otm_state = fields.Selection([
        ('planning', 'Planning'), ('in_progress', 'In Progress'), ('on_hold', 'On Hold'),
        ('delivered', 'Delivered'), ('closed', 'Closed'), ('cancelled', 'Cancelled'),
    ], string='Lifecycle Status', copy=False, tracking=True, index=True)
    otm_deal_id = fields.Many2one('otm.deal', string='Deal', readonly=True, copy=False, groups=SALES_GROUPS)
    otm_lead_id = fields.Many2one('otm.lead', string='Lead', readonly=True, copy=False, groups=SALES_GROUPS)
    otm_agreement_id = fields.Many2one(
        'otm.customer.agreement', string='Agreement', readonly=True, copy=False, groups=SALES_GROUPS)
    otm_estimate_id = fields.Many2one(
        'otm.estimate', string='Estimate', readonly=True, copy=False, groups=SALES_GROUPS)
    otm_sales_team_id = fields.Many2one('otm.sales.team', string='Sales Team', readonly=True, index=True, copy=False)
    otm_sales_head_id = fields.Many2one('res.users', string='Sales Head', readonly=True, index=True, copy=False)
    otm_salesperson_id = fields.Many2one('res.users', string='Sales Executive', readonly=True, index=True, copy=False)
    # approved information handed to the developers (never amounts)
    otm_scope = fields.Text(string='Approved Scope', readonly=True, copy=False)
    otm_deliverables = fields.Text(string='Deliverables', readonly=True, copy=False)
    otm_requirements = fields.Text(string='Customer Requirements', readonly=True, copy=False)
    otm_services = fields.Text(string='Approved Services', readonly=True, copy=False)
    otm_customizations = fields.Text(string='Approved Customizations', readonly=True, copy=False)
    otm_start_date = fields.Date(string='Project Start Date', tracking=True, copy=False)
    otm_technical_requirements = fields.Text(
        string='Technical Requirements',
        groups='sales_project_lifecycle.group_developer,sales_project_lifecycle.group_qc,'
               'sales_project_lifecycle.group_project_head')
    # team
    otm_developer_ids = fields.Many2many(
        'res.users', 'otm_project_developer_rel', 'project_id', 'user_id', string='Developers')
    otm_qc_user_id = fields.Many2one('res.users', string='QC User', tracking=True)
    otm_deploy_user_id = fields.Many2one('res.users', string='Deployment Responsible', tracking=True)
    otm_trainer_id = fields.Many2one('res.users', string='Trainer', tracking=True)
    otm_delivered_date = fields.Datetime(string='Final Delivery Date', readonly=True, copy=False)
    otm_delivered_by_id = fields.Many2one('res.users', string='Delivered By', readonly=True, copy=False)
    otm_closed_date = fields.Datetime(string='Closed Date', readonly=True, copy=False)
    otm_closed_by_id = fields.Many2one('res.users', string='Closed By', readonly=True, copy=False)
    otm_stage_tracker = fields.Json(string='Lifecycle Progress', compute='_compute_otm_stage_tracker')
    otm_review_ids = fields.One2many('otm.customer.review', 'project_id', string='Customer Reviews')
    otm_qc_ids = fields.One2many('otm.qc', 'project_id', string='QC Rounds')
    otm_issue_ids = fields.One2many('otm.qc.issue', 'project_id', string='QC Issues')
    otm_deployment_ids = fields.One2many('otm.deployment', 'project_id', string='Deployments')
    otm_training_ids = fields.One2many('otm.training', 'project_id', string='Training Sessions')
    otm_training_done = fields.Boolean(compute='_compute_otm_training_done', string='Required Training Completed')
    otm_qc_state = fields.Selection([
        ('none', 'Not submitted'), ('pending', 'Pending'), ('testing', 'Testing'),
        ('passed', 'Passed'), ('failed', 'Failed')], compute='_compute_otm_qc_state', string='QC Status')
    otm_open_issue_count = fields.Integer(compute='_compute_otm_qc_state')
    # stages
    otm_stage_ids = fields.One2many('otm.project.stage.line', 'project_id', string='Lifecycle Stages')
    otm_current_stage_id = fields.Many2one(
        'otm.project.stage.line', string='Current Stage', compute='_compute_otm_current_stage')
    otm_progress = fields.Float(string='Lifecycle Progress (%)', compute='_compute_otm_current_stage')
    otm_delay_days = fields.Integer(string='Delay (days)', compute='_compute_otm_current_stage')
    otm_planned_end = fields.Date(string='Planned Completion', compute='_compute_otm_current_stage')

    @api.depends('otm_training_ids.status', 'otm_training_ids.required')
    def _compute_otm_training_done(self):
        for p in self:
            req = p.sudo().otm_training_ids.filtered(lambda t: t.required and t.status != 'cancelled')
            p.otm_training_done = bool(req) and all(t.status == 'completed' for t in req)

    def _otm_check_training_done(self):
        """All required sessions completed -> the training installment (30%) falls due."""
        for p in self.sudo():
            if p.otm_training_done and p.otm_deal_id:
                p.otm_deal_id._otm_payment_event('after_training')

    @api.depends('otm_qc_ids.status', 'otm_issue_ids.status')
    def _compute_otm_qc_state(self):
        for p in self:
            last = p.sudo().otm_qc_ids.sorted('id')[-1:] 
            p.otm_qc_state = last.status if last else 'none'
            p.otm_open_issue_count = len(p.sudo().otm_issue_ids.filtered(
                lambda i: i.status in ('open', 'assigned', 'fixed', 'retest')))

    @api.depends('otm_stage_ids.state', 'otm_stage_ids.planned_deadline', 'otm_stage_ids.delay_days')
    def _compute_otm_current_stage(self):
        for project in self:
            lines = project.otm_stage_ids.sorted(lambda l: (l.sequence, l.id))
            current = lines.filtered(lambda l: l.state == 'in_progress')[:1] \
                or lines.filtered(lambda l: l.state == 'pending')[:1]
            project.otm_current_stage_id = current
            done = len(lines.filtered(lambda l: l.state in ('done', 'skipped')))
            project.otm_progress = (done / len(lines) * 100.0) if lines else 0.0
            project.otm_delay_days = max(lines.mapped('delay_days') or [0])
            project.otm_planned_end = max(lines.mapped('planned_deadline') or [False]) or False

    # ------------------------------------------------------------------
    # Access guards
    # ------------------------------------------------------------------
    @api.model_create_multi
    def create(self, vals_list):
        if not self.env.su:
            for vals in vals_list:
                if PROTECTED & vals.keys():
                    raise UserError(_("Lifecycle fields are set automatically from the approved deal."))
        return super().create(vals_list)

    def write(self, vals):
        lifecycle = self.filtered('otm_is_lifecycle')
        if lifecycle and not (self.env.su or self.env.context.get('otm_transition')):
            if PROTECTED & vals.keys():
                raise UserError(_(
                    "The approved scope and commercial links of a lifecycle project are protected."))
            if TEAM_FIELDS & vals.keys():
                for project in lifecycle:
                    project._otm_check_project_head()
                    if project.otm_state in ('closed', 'cancelled'):
                        raise UserError(_("A closed or cancelled project cannot be changed."))
        old_devs = {p.id: p.otm_developer_ids.mapped('name') for p in lifecycle} \
            if 'otm_developer_ids' in vals else {}
        res = super().write(vals)
        for project in lifecycle.filtered(lambda p: p.id in old_devs):
            new = project.otm_developer_ids.mapped('name')
            if new != old_devs[project.id]:
                project.message_post(body=_("Developers changed: %(old)s → %(new)s",
                                            old=', '.join(old_devs[project.id]) or '-', new=', '.join(new) or '-'))
        if lifecycle and not self.env.context.get('otm_recompute'):
            if {'otm_start_date', 'otm_stage_ids'} & vals.keys():
                lifecycle._otm_recompute_stage_dates()
            if {'user_id', 'otm_developer_ids', 'otm_qc_user_id', 'otm_deploy_user_id',
                    'otm_trainer_id'} & vals.keys():
                lifecycle._otm_sync_followers()
        return res

    def _otm_check_project_head(self):
        self.ensure_one()
        user = self.env.user
        if self.env.su or user.has_group(ADMIN):
            return
        ok = user == self.user_id if self.user_id else user.has_group(PROJECT_HEAD)
        if not ok:
            raise AccessError(_("Only the Project Head of this project or an Administrator can do this."))

    def unlink(self):
        if self.filtered('otm_is_lifecycle') and not self.env.su:
            raise UserError(_("A lifecycle project cannot be deleted. Cancel it instead."))
        return super().unlink()

    @api.constrains('otm_developer_ids', 'user_id')
    def _check_team_users(self):
        dev = self.env.ref('sales_project_lifecycle.group_developer', raise_if_not_found=False)
        for project in self.filtered('otm_is_lifecycle'):
            for user in project.sudo().otm_developer_ids:
                if dev and not user.has_group('sales_project_lifecycle.group_developer'):
                    raise ValidationError(_("%s does not have the Developer role.", user.name))

    def _otm_sync_followers(self):
        for project in self.sudo():
            # developers are deliberately NOT followers: followers of the project
            # see all its tasks, developers must only see the tasks assigned to them
            users = (project.user_id | project.otm_qc_user_id
                     | project.otm_deploy_user_id | project.otm_trainer_id)
            project.message_subscribe(partner_ids=users.partner_id.ids)

    # ------------------------------------------------------------------
    # Stage dates
    # ------------------------------------------------------------------
    def _otm_recompute_stage_dates(self):
        """planned_start / deadline = project start + cumulated durations.

        A stage whose dates the Project Head overrode keeps its dates and the
        following stages continue from its deadline.
        """
        for project in self.sudo():
            start = project.otm_start_date
            lines = project.otm_stage_ids.sorted(lambda l: (l.sequence, l.id))
            if not start:
                continue
            cursor = start
            for line in lines:
                if line.date_override and line.planned_start and line.planned_deadline:
                    cursor = line.planned_deadline
                    continue
                p_start = cursor
                p_end = cursor + timedelta(days=line.duration_days)
                line.with_context(otm_recompute=True, otm_transition=True).write({
                    'planned_start': p_start, 'planned_deadline': p_end})
                cursor = p_end

    def _otm_create_stage_lines(self):
        Template = self.env['otm.project.stage'].sudo()
        Line = self.env['otm.project.stage.line'].sudo()
        for project in self:
            for tmpl in Template.search([('active', '=', True)]):
                Line.create({
                    'project_id': project.id, 'template_id': tmpl.id, 'name': tmpl.name,
                    'sequence': tmpl.sequence, 'duration_days': tmpl.duration_days,
                    'responsible_role': tmpl.responsible_role, 'required': tmpl.required})

    # ------------------------------------------------------------------
    # Transition engine
    # ------------------------------------------------------------------
    def _otm_log_scope(self):
        return (self.otm_sales_team_id, self.otm_sales_head_id)

    def _otm_check_actor(self, action):
        self.ensure_one()
        self._otm_check_project_head()

    def _otm_prerequisites(self, action):
        self.ensure_one()
        missing = []
        if action == 'deliver':
            missing += self._otm_delivery_blockers()
        if action == 'close':
            missing += self._otm_closure_blockers()
        if action == 'start':
            if not self.user_id:
                missing.append(_("A Project Head must be assigned."))
            if not self.otm_start_date:
                missing.append(_("The project start date is required."))
            if not self.otm_developer_ids:
                missing.append(_("At least one developer must be assigned."))
            if not self.otm_stage_ids:
                missing.append(_("The project has no lifecycle stages."))
            deal = self.sudo().otm_deal_id
            if not deal.project_start_allowed:
                missing.append(_(deal.PROJECT_START_MESSAGE))
        return missing

    def _compute_otm_stage_tracker(self):
        Dash = self.env['otm.dashboard']
        for p in self:
            lead = p.sudo().otm_lead_id
            p.otm_stage_tracker = Dash.lifecycle_tracker(lead) if lead else False

    def _otm_delivery_blockers(self):
        """Final delivery: training done (or not required), 30% received, customer verification done."""
        self.ensure_one()
        p, out = self.sudo(), []
        required = p.otm_training_ids.filtered(lambda t: t.required and t.status != 'cancelled')
        if required and not p.otm_training_done:
            out.append(_("The required training is not completed."))
        pays = p.otm_deal_id.payment_ids.filtered(lambda x: x.status != 'cancelled')
        triggers = ('advance', 'after_training') if required else ('advance',)
        for pay in pays.filtered(lambda x: x.trigger in triggers and x.status != 'received'):
            out.append(_("The '%s' payment has not been received.", pay.name))
        if not p.otm_deployment_ids.filtered(lambda d: d.status == 'completed'):
            out.append(_("The customer verification of the deployment is not completed."))
        return out

    # ------------------------------------------------------------------
    # Stage checklist follows the real work (QC, deployment, training, payments, review ...)
    # ------------------------------------------------------------------
    _OTM_MILESTONE_STAGE = {
        'development': r'\bdevelopment\b', 'testing': r'internal test', 'qc': r'^qc$|quality',
        'deployment': r'^deployment$', 'verification': r'verification', 'training': r'^training$',
        'pay30': r'30\s*%', 'delivery': r'final delivery', 'pay20': r'20\s*%',
        'review': r'customer review', 'closed': r'^completed$|^closure$',
    }

    def _otm_sync_stages(self, milestone):
        """Mark the stage that matches a real milestone (and every earlier stage) as done, with audit entries.

        The manual Start / Complete buttons still exist, but nobody has to press them for work that Odoo
        already knows is finished.
        """
        import re
        pattern = self._OTM_MILESTONE_STAGE.get(milestone)
        if not pattern:
            return
        Log = self.env['otm.transition.log'].sudo()
        now = fields.Datetime.now()
        for project in self.sudo():
            lines = project.otm_stage_ids.sorted(lambda l: (l.sequence, l.id))
            targets = lines.filtered(lambda l: re.search(pattern, (l.name or '').strip().lower()))
            if not targets:
                continue
            last_seq = max(targets.mapped('sequence'))
            for line in lines.filtered(lambda l: l.sequence <= last_seq and l.state in ('pending', 'in_progress')):
                old = line.state
                vals = {'state': 'done', 'actual_end': now}
                if not line.actual_start:
                    vals['actual_start'] = now
                line.with_context(otm_transition=True).write(vals)
                Log.create({
                    'res_model': line._name, 'res_id': line.id, 'record_name': line.display_name,
                    'action': 'auto_complete', 'from_state': line._otm_state_label(old, 'state'),
                    'to_state': line._otm_state_label('done', 'state'), 'user_id': self.env.user.id,
                    'reason': _("Completed automatically: %s", milestone.replace('_', ' ')),
                    'sales_team_id': project.otm_sales_team_id.id, 'owner_id': project.otm_sales_head_id.id})

    def _otm_closure_blockers(self):
        self.ensure_one()
        p, out = self.sudo(), []
        last = p.otm_qc_ids.sorted('id')[-1:]
        if not last or last.status != 'passed':
            out.append(_("The latest QC round is not passed."))
        if p.otm_issue_ids.filtered(lambda i: i.status in ('open', 'assigned', 'fixed', 'retest')):
            out.append(_("There are unresolved QC issues."))
        if not p.otm_deployment_ids.filtered(lambda d: d.status == 'completed'):
            out.append(_("No completed deployment with customer verification."))
        required = p.otm_training_ids.filtered(lambda t: t.required and t.status != 'cancelled')
        if required and not p.otm_training_done:
            out.append(_("The required training is not completed."))
        pays = p.otm_deal_id.payment_ids.filtered(lambda x: x.status != 'cancelled')
        for pay in pays.filtered(lambda x: x.status != 'received'):
            out.append(_("The '%(n)s' payment (%(a)s) has not been received.",
                         n=pay.name, a=pay.currency_id.format(pay.amount)))
        if not pays:
            out.append(_("The payment schedule has no payments."))
        return out

    def _otm_create_client_services(self):
        """After closure the purchased services stay active as client services (one per deal line)."""
        Svc = self.env['otm.client.service'].sudo()
        for p in self.sudo():
            deal = p.otm_deal_id
            if not deal or Svc.search_count([('deal_id', '=', deal.id)]):
                continue
            for line in deal.line_ids:
                Svc.create({
                    'customer_id': p.partner_id.id or deal.customer_id.id, 'project_id': p.id,
                    'deal_id': deal.id, 'service_id': line.service_id.id,
                    'service_name': line.description or line.service_id.name, 'service_kind': 'solution',
                    'amount': line.selling_subtotal, 'billing_type': 'one_time',
                    'start_date': fields.Date.context_today(p), 'sales_team_id': p.otm_sales_team_id.id,
                    'responsible_user_id': p.otm_salesperson_id.id})

    def _otm_request_review(self):
        Review = self.env['otm.customer.review'].sudo()
        for p in self.sudo():
            if not Review.search_count([('project_id', '=', p.id)]):
                Review.create({
                    'project_id': p.id, 'customer_id': p.partner_id.id,
                    'sales_team_id': p.otm_sales_team_id.id, 'sales_head_id': p.otm_sales_head_id.id,
                    'salesperson_id': p.otm_salesperson_id.id})
                p.message_post(body=_("Customer review requested."))

    def _otm_after_transition(self, action, old_state, reason):
        for project in self:
            if action == 'start':
                first = project.otm_stage_ids.sorted(lambda l: (l.sequence, l.id))[:1]
                if first and first.state == 'pending':
                    first._otm_do_transition('start')  # audited like any manual start
            elif action == 'deliver':
                project.with_context(otm_transition=True).write({
                    'otm_delivered_date': fields.Datetime.now(), 'otm_delivered_by_id': self.env.user.id})
                if not project.sudo().otm_training_ids.filtered(lambda t: t.required and t.status != 'cancelled'):
                    project.sudo().otm_deal_id._otm_payment_event('after_training')
                project.sudo().otm_deal_id._otm_payment_event('final_delivery')
                project._otm_sync_stages('delivery')
            elif action == 'close':
                project.with_context(otm_transition=True).write({
                    'otm_closed_date': fields.Datetime.now(), 'otm_closed_by_id': self.env.user.id})
                project._otm_sync_stages('closed')
                lead = project.sudo().otm_lead_id
                if lead.stage == 'project':
                    lead._otm_do_transition('system_won')
                project.sudo().otm_deal_id._otm_commission_event('project_completed')
                project._otm_create_client_services()
            elif action == 'cancel':
                project.otm_stage_ids.filtered(lambda l: l.state == 'in_progress').with_context(
                    otm_transition=True).write({'state': 'pending', 'actual_start': False})

    def _otm_require_reason(self, reason):
        if not (reason or '').strip():
            raise UserError(_("A reason is required for this action."))

    def action_otm_start(self):
        return self._otm_do_transition('start')

    def action_final_delivery(self):
        return self._otm_do_transition('deliver')

    def action_close(self):
        return self._otm_do_transition('close')

    def action_otm_open_reviews(self):
        self.ensure_one()
        return {'type': 'ir.actions.act_window', 'name': _('Reviews'), 'res_model': 'otm.customer.review',
                'view_mode': 'list,form', 'domain': [('project_id', '=', self.id)]}

    def action_hold(self, reason=None):
        self._otm_require_reason(reason)
        return self._otm_do_transition('hold', reason=reason)

    def action_otm_resume(self):
        return self._otm_do_transition('resume')

    def action_cancel(self, reason=None):
        self._otm_require_reason(reason)
        return self._otm_do_transition('cancel', reason=reason)

    def action_submit_qc(self):
        """Project Head sends the finished development to QC (new round / resubmission)."""
        self.ensure_one()
        self._otm_check_project_head()
        p = self.sudo()
        errors = []
        if p.otm_state != 'in_progress':
            errors.append(_("The project is not in progress."))
        tasks = p.task_ids.filtered(lambda t: t.otm_lifecycle)
        if not tasks:
            errors.append(_("The project has no development tasks."))
        for t in tasks.filtered(lambda t: t.otm_dev_status != 'completed'):
            errors.append(_("Task '%s' is not completed.", t.name))
        if not p.otm_qc_user_id:
            errors.append(_("Assign a QC user to the project."))
        last = p.otm_qc_ids.sorted('id')[-1:]
        if last and last.status in ('pending', 'testing'):
            errors.append(_("QC round %s is still running.", last.name))
        if last and last.status == 'failed':
            for i in p.otm_issue_ids.filtered(lambda i: i.status in ('open', 'assigned')):
                errors.append(_("Issue '%s' has not been fixed yet.", i.title))
        if last and last.status == 'passed' and not p.otm_issue_ids.filtered(
                lambda i: i.status in ('open', 'assigned', 'fixed', 'retest')):
            errors.append(_("The latest QC round already passed."))
        if errors:
            raise UserError(_("The project cannot be sent to QC:\n%s", '\n'.join('- ' + e for e in errors)))
        qc = self.env['otm.qc'].sudo().create({
            'project_id': p.id, 'round_number': len(p.otm_qc_ids) + 1,
            'submitted_by_id': self.env.user.id, 'submitted_date': fields.Datetime.now()})
        p.otm_issue_ids.filtered(lambda i: i.status == 'fixed')._otm_do_transition('system_retest')
        p._otm_sync_stages('testing')
        self.env['otm.transition.log'].sudo().create({
            'res_model': 'project.project', 'res_id': p.id, 'record_name': p.display_name,
            'action': 'submit_qc', 'from_state': _('Development'), 'to_state': qc.name,
            'user_id': self.env.user.id, 'sales_team_id': p.otm_sales_team_id.id,
            'owner_id': p.otm_sales_head_id.id})
        return {'type': 'ir.actions.act_window', 'res_model': 'otm.qc', 'res_id': qc.id, 'view_mode': 'form'}

    def action_otm_open_trainings(self):
        self.ensure_one()
        return {'type': 'ir.actions.act_window', 'name': _('Training'), 'res_model': 'otm.training',
                'view_mode': 'list,form', 'domain': [('project_id', '=', self.id)],
                'context': {'default_project_id': self.id}}

    def action_otm_open_qc(self):
        self.ensure_one()
        return {'type': 'ir.actions.act_window', 'name': _('QC'), 'res_model': 'otm.qc',
                'view_mode': 'list,form', 'domain': [('project_id', '=', self.id)]}

    def action_otm_open_issues(self):
        self.ensure_one()
        return {'type': 'ir.actions.act_window', 'name': _('QC Issues'), 'res_model': 'otm.qc.issue',
                'view_mode': 'list,form', 'domain': [('project_id', '=', self.id)],
                'context': {'default_project_id': self.id}}

    def action_otm_open_deployments(self):
        self.ensure_one()
        return {'type': 'ir.actions.act_window', 'name': _('Deployments'), 'res_model': 'otm.deployment',
                'view_mode': 'list,form', 'domain': [('project_id', '=', self.id)],
                'context': {'default_project_id': self.id}}

    def action_otm_open_history(self):
        self.ensure_one()
        return {'type': 'ir.actions.act_window', 'name': _('History'),
                'res_model': 'otm.transition.log', 'view_mode': 'list,form',
                'domain': ['|',
                           '&', ('res_model', '=', 'project.project'), ('res_id', '=', self.id),
                           '&', ('res_model', '=', 'otm.project.stage.line'),
                           ('res_id', 'in', self.otm_stage_ids.ids)]}
