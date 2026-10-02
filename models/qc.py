from odoo import _, api, fields, models
from odoo.exceptions import AccessError, UserError, ValidationError

ADMIN = 'sales_project_lifecycle.group_lifecycle_admin'
OPEN_ISSUE = ('open', 'assigned', 'fixed', 'retest')
SEVERITY = [('critical', 'Critical'), ('high', 'High'), ('medium', 'Medium'), ('low', 'Low')]

QC_MATRIX = {
    'start': {'from': ('pending',), 'to': 'testing'},
    'pass': {'from': ('testing',), 'to': 'passed'},
    'fail': {'from': ('testing',), 'to': 'failed'},
}
ISSUE_MATRIX = {
    'assign': {'from': ('open',), 'to': 'assigned'},
    'fix': {'from': ('assigned',), 'to': 'fixed'},
    'system_retest': {'from': ('fixed',), 'to': 'retest', 'system': True},
    'pass': {'from': ('retest',), 'to': 'passed'},
    'fail': {'from': ('retest',), 'to': 'assigned'},
    'reject': {'from': ('open', 'assigned'), 'to': 'rejected'},
}


class OtmQc(models.Model):
    _name = 'otm.qc'
    _description = 'QC Round'
    _inherit = ['mail.thread', 'otm.transition.mixin']
    _order = 'id desc'
    _otm_state_field = 'status'
    _otm_matrix = QC_MATRIX

    name = fields.Char(string='Reference', readonly=True, copy=False, default=lambda s: _('New'))
    project_id = fields.Many2one('project.project', required=True, index=True, ondelete='restrict', readonly=True)
    round_number = fields.Integer(readonly=True)
    sales_team_id = fields.Many2one('otm.sales.team', related='project_id.otm_sales_team_id', store=True)
    sales_head_id = fields.Many2one('res.users', related='project_id.otm_sales_head_id', store=True)
    status = fields.Selection([
        ('pending', 'Pending'), ('testing', 'Testing'), ('passed', 'Passed'), ('failed', 'Failed'),
    ], default='pending', required=True, tracking=True, index=True, copy=False)
    submitted_by_id = fields.Many2one('res.users', readonly=True)
    submitted_date = fields.Datetime(readonly=True)
    started_date = fields.Datetime(readonly=True)
    finished_date = fields.Datetime(readonly=True)
    tester_id = fields.Many2one('res.users', string='Tester', readonly=True)
    notes = fields.Text(string='QC Notes')
    issue_ids = fields.One2many('otm.qc.issue', 'qc_id', string='Issues Raised')
    issue_count = fields.Integer(compute='_compute_issue_count')

    @api.depends('issue_ids')
    def _compute_issue_count(self):
        for qc in self:
            qc.issue_count = len(qc.issue_ids)

    @api.model_create_multi
    def create(self, vals_list):
        if not self.env.su:
            raise UserError(_("QC rounds are created with 'Submit for QC' on the project."))
        seq = self.env['ir.sequence'].sudo()
        for vals in vals_list:
            vals['name'] = seq.next_by_code('otm.qc') or _('New')
        return super().create(vals_list)

    def write(self, vals):
        if not (self.env.su or self.env.context.get('otm_transition')) \
                and set(vals) - {'notes', 'message_main_attachment_id'}:
            raise UserError(_("QC values can only change through the workflow."))
        return super().write(vals)

    def unlink(self):
        if not self.env.su:
            raise UserError(_("QC history cannot be deleted."))
        return super().unlink()

    def _otm_log_scope(self):
        return (self.sales_team_id, self.sales_head_id)

    def _otm_check_actor(self, action):
        self.ensure_one()
        user = self.env.user
        if self.env.su or user.has_group(ADMIN):
            return
        if user != self.project_id.sudo().otm_qc_user_id:
            raise AccessError(_("Only the QC user assigned to this project can do this."))

    def _otm_prerequisites(self, action):
        self.ensure_one()
        missing = []
        issues = self.sudo().project_id.otm_issue_ids
        if action == 'start':
            if self.sudo().project_id.otm_state != 'in_progress':
                missing.append(_("The project is not in progress."))
        if action == 'pass':
            for i in issues.filtered(lambda i: i.status in OPEN_ISSUE):
                missing.append(_("Issue '%(t)s' is still %(s)s.", t=i.title, s=i._otm_state_label(i.status)))
        if action == 'fail':
            if not issues.filtered(lambda i: i.status in OPEN_ISSUE or (
                    i.qc_id == self and i.status != 'rejected')):
                missing.append(_("Raise at least one issue (or fail a retest) before failing the QC."))
        return missing

    def _otm_after_transition(self, action, old_state, reason):
        now = fields.Datetime.now()
        for qc in self:
            w = qc.with_context(otm_transition=True)
            if action == 'start':
                w.write({'started_date': now, 'tester_id': self.env.user.id})
            else:
                w.write({'finished_date': now})

    def action_start(self):
        return self._otm_do_transition('start')

    def action_pass(self):
        return self._otm_do_transition('pass')

    def action_fail(self):
        return self._otm_do_transition('fail')

    def action_open_issues(self):
        self.ensure_one()
        return {'type': 'ir.actions.act_window', 'name': _('Issues'), 'res_model': 'otm.qc.issue',
                'view_mode': 'list,form', 'domain': [('qc_id', '=', self.id)],
                'context': {'default_project_id': self.project_id.id, 'default_qc_id': self.id}}


class OtmQcIssue(models.Model):
    _name = 'otm.qc.issue'
    _description = 'QC Issue'
    _inherit = ['mail.thread', 'otm.transition.mixin']
    _order = 'id desc'
    _otm_state_field = 'status'
    _otm_matrix = ISSUE_MATRIX
    _otm_reason_methods = ('action_fail', 'action_reject')

    name = fields.Char(string='Reference', readonly=True, copy=False, default=lambda s: _('New'))
    title = fields.Char(required=True, tracking=True)
    description = fields.Text()
    project_id = fields.Many2one('project.project', required=True, index=True, ondelete='restrict')
    qc_id = fields.Many2one('otm.qc', string='QC Round', index=True, ondelete='restrict')
    stage_line_id = fields.Many2one('otm.project.stage.line', string='Stage', domain="[('project_id', '=', project_id)]")
    source = fields.Selection([('qc', 'QC'), ('customer', 'Customer Verification')], default='qc', readonly=True)
    severity = fields.Selection(SEVERITY, required=True, default='medium', tracking=True)
    assigned_developer_id = fields.Many2one('res.users', string='Assigned Developer', tracking=True, index=True)
    sales_team_id = fields.Many2one('otm.sales.team', related='project_id.otm_sales_team_id', store=True)
    sales_head_id = fields.Many2one('res.users', related='project_id.otm_sales_head_id', store=True)
    status = fields.Selection([
        ('open', 'Open'), ('assigned', 'Assigned'), ('fixed', 'Fixed'), ('retest', 'Retest'),
        ('passed', 'Passed'), ('rejected', 'Rejected'),
    ], default='open', required=True, tracking=True, index=True, copy=False)
    created_date = fields.Datetime(default=fields.Datetime.now, readonly=True)
    created_by_id = fields.Many2one('res.users', default=lambda s: s.env.user, readonly=True)
    resolved_date = fields.Datetime(readonly=True, copy=False)
    resolution_notes = fields.Text()
    history_ids = fields.One2many(
        'otm.transition.log', compute='_compute_history', string='History')

    def _compute_history(self):
        Log = self.env['otm.transition.log']
        for issue in self:
            issue.history_ids = Log.search([('res_model', '=', self._name), ('res_id', '=', issue.id)])

    def _otm_is_head_or_admin(self, user=None):
        user = user or self.env.user
        return self.env.su or user.has_group(ADMIN) or user == self.sudo().project_id.user_id

    def _otm_is_qc(self, user=None):
        user = user or self.env.user
        return user == self.sudo().project_id.otm_qc_user_id

    @api.model_create_multi
    def create(self, vals_list):
        seq = self.env['ir.sequence'].sudo()
        for vals in vals_list:
            vals['name'] = seq.next_by_code('otm.qc.issue') or _('New')
            if not self.env.su:
                project = self.env['project.project'].sudo().browse(vals.get('project_id'))
                user = self.env.user
                if not (user.has_group(ADMIN) or user in (project.user_id | project.otm_qc_user_id)):
                    raise AccessError(_("Only the QC user or the Project Head can raise an issue."))
                if project.otm_state != 'in_progress':
                    raise UserError(_("Issues can only be raised on a project in progress."))
                for f in ('status', 'source', 'resolved_date'):
                    vals.pop(f, None)
        issues = super().create(vals_list)
        issues._otm_check_developer()
        return issues

    def write(self, vals):
        if not (self.env.su or self.env.context.get('otm_transition')):
            if 'status' in vals:
                raise UserError(_("The issue status cannot be edited directly. Use the workflow buttons."))
            blocked = {'project_id', 'qc_id', 'source', 'created_date', 'created_by_id', 'resolved_date'}
            if blocked & vals.keys():
                raise UserError(_("These issue fields cannot be changed."))
            for issue in self:
                if issue._otm_is_head_or_admin() or issue._otm_is_qc():
                    if issue.status in ('passed', 'rejected'):
                        raise UserError(_("A closed issue cannot be changed."))
                    if 'assigned_developer_id' in vals and not issue._otm_is_head_or_admin():
                        raise AccessError(_("Only the Project Head assigns developers."))
                    continue
                if self.env.user == issue.assigned_developer_id:
                    if set(vals) - {'resolution_notes'}:
                        raise AccessError(_("Developers can only enter resolution notes."))
                    if issue.status != 'assigned':
                        raise UserError(_("Resolution notes can only be entered while the issue is assigned to you."))
                    continue
                raise AccessError(_("You are not allowed to change this issue."))
        res = super().write(vals)
        if 'assigned_developer_id' in vals:
            self._otm_check_developer()
        return res

    def unlink(self):
        if not self.env.su:
            raise UserError(_("Issues cannot be deleted. Reject them instead."))
        return super().unlink()

    def _otm_check_developer(self):
        for issue in self.sudo():
            dev = issue.assigned_developer_id
            if dev and dev not in issue.project_id.otm_developer_ids:
                raise ValidationError(_("%s is not a developer of this project.", dev.name))

    def _otm_log_scope(self):
        return (self.sales_team_id, self.sales_head_id)

    def _otm_check_actor(self, action):
        self.ensure_one()
        user = self.env.user
        if self.env.su or user.has_group(ADMIN):
            return
        head, qc = user == self.project_id.sudo().user_id, self._otm_is_qc()
        if action == 'assign':
            ok = head
        elif action == 'fix':
            ok = user == self.assigned_developer_id or head
        elif action in ('pass', 'fail'):
            ok = qc
        else:  # reject
            ok = qc or head
        if not ok:
            raise AccessError(_("You are not allowed to perform this action on this issue."))

    def _otm_prerequisites(self, action):
        self.ensure_one()
        missing = []
        if action == 'assign' and not self.assigned_developer_id:
            missing.append(_("Select the developer first."))
        if action == 'fix' and not (self.resolution_notes or '').strip():
            missing.append(_("Resolution notes are required."))
        return missing

    def _otm_after_transition(self, action, old_state, reason):
        now = fields.Datetime.now()
        for issue in self:
            if action in ('pass', 'reject'):
                issue.with_context(otm_transition=True).write({'resolved_date': now})
            elif action == 'fail':
                issue.with_context(otm_transition=True).write({'resolved_date': False})

    def _otm_require_reason(self, reason):
        if not (reason or '').strip():
            raise UserError(_("A reason is required for this action."))

    def action_assign(self):
        return self._otm_do_transition('assign')

    def action_fix(self):
        return self._otm_do_transition('fix')

    def action_pass(self):
        return self._otm_do_transition('pass')

    def action_fail(self, reason=None):
        self._otm_require_reason(reason)
        return self._otm_do_transition('fail', reason=reason)

    def action_reject(self, reason=None):
        self._otm_require_reason(reason)
        return self._otm_do_transition('reject', reason=reason)
