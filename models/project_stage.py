from datetime import timedelta

from odoo import _, api, fields, models
from odoo.exceptions import AccessError, UserError, ValidationError

ROLES = [
    ('project_head', 'Project Head'), ('developer', 'Developer'), ('qc', 'QC'),
    ('deployment', 'Deployment'), ('trainer', 'Trainer'), ('finance', 'Finance'),
    ('sales', 'Sales'), ('customer', 'Customer'),
]
ROLE_FIELD = {
    'project_head': 'user_id', 'qc': 'otm_qc_user_id',
    'deployment': 'otm_deploy_user_id', 'trainer': 'otm_trainer_id',
}

STAGE_MATRIX = {
    'start': {'from': ('pending',), 'to': 'in_progress'},
    'complete': {'from': ('in_progress',), 'to': 'done'},
    'skip': {'from': ('pending',), 'to': 'skipped'},
    'reopen': {'from': ('done', 'skipped'), 'to': 'pending'},
}


class OtmProjectStage(models.Model):
    """Configurable template of lifecycle stages copied into each new project."""
    _name = 'otm.project.stage'
    _description = 'Project Stage Template'
    _order = 'sequence, id'

    name = fields.Char(required=True, translate=True)
    sequence = fields.Integer(default=10)
    duration_days = fields.Integer(string='Duration (days)', default=1)
    responsible_role = fields.Selection(ROLES, string='Responsible Role', default='project_head')
    required = fields.Boolean(default=True)
    active = fields.Boolean(default=True)

    @api.constrains('duration_days')
    def _check_duration(self):
        for st in self:
            if st.duration_days < 0:
                raise ValidationError(_("The stage duration cannot be negative."))


class OtmProjectStageLine(models.Model):
    """A stage instance of one project, with planned/actual dates."""
    _name = 'otm.project.stage.line'
    _description = 'Project Lifecycle Stage'
    _inherit = ['otm.transition.mixin']
    _order = 'project_id, sequence, id'
    _otm_state_field = 'state'
    _otm_matrix = STAGE_MATRIX
    _otm_reason_methods = ('action_skip', 'action_reopen')

    project_id = fields.Many2one('project.project', required=True, ondelete='cascade', index=True)
    template_id = fields.Many2one('otm.project.stage', ondelete='set null')
    name = fields.Char(required=True)
    sequence = fields.Integer(default=10)
    duration_days = fields.Integer(string='Duration (days)', default=1)
    responsible_role = fields.Selection(ROLES, default='project_head')
    responsible_user_id = fields.Many2one(
        'res.users', string='Responsible', compute='_compute_responsible')
    required = fields.Boolean(default=True)
    state = fields.Selection([
        ('pending', 'Pending'), ('in_progress', 'In Progress'),
        ('done', 'Done'), ('skipped', 'Skipped'),
    ], default='pending', required=True, index=True, copy=False)
    date_override = fields.Boolean(
        string='Dates Overridden', copy=False,
        help="Set when the Project Head changed the planned dates by hand.")
    planned_start = fields.Date()
    planned_deadline = fields.Date()
    actual_start = fields.Datetime(readonly=True, copy=False)
    actual_end = fields.Datetime(readonly=True, copy=False)
    delay_days = fields.Integer(compute='_compute_delay')
    custom = fields.Boolean(string='Custom Stage', default=False)

    @api.depends('responsible_role', 'project_id.user_id', 'project_id.otm_qc_user_id',
                 'project_id.otm_deploy_user_id', 'project_id.otm_trainer_id')
    def _compute_responsible(self):
        for line in self:
            fname = ROLE_FIELD.get(line.responsible_role)
            line.responsible_user_id = line.project_id[fname] if fname else False

    @api.depends('planned_deadline', 'actual_end', 'state')
    def _compute_delay(self):
        today = fields.Date.context_today(self)
        for line in self:
            delay = 0
            if line.planned_deadline:
                if line.state == 'done' and line.actual_end:
                    delay = (line.actual_end.date() - line.planned_deadline).days
                elif line.state in ('pending', 'in_progress'):
                    delay = (today - line.planned_deadline).days
            line.delay_days = max(delay, 0)

    # ------------------------------------------------------------------
    def _otm_project_head_check(self):
        project = self.project_id
        user = self.env.user
        if self.env.su or user.has_group('sales_project_lifecycle.group_lifecycle_admin'):
            return
        head = project.user_id
        if head:
            ok = user == head
        else:
            ok = user.has_group('sales_project_lifecycle.group_project_head')
        if not ok:
            raise AccessError(_("Only the Project Head of this project or an Administrator can do this."))

    @api.model_create_multi
    def create(self, vals_list):
        if not self.env.su:
            for vals in vals_list:
                project = self.env['project.project'].browse(vals.get('project_id'))
                self.new({'project_id': project.id})._otm_project_head_check()
                if project.otm_state in ('closed', 'cancelled'):
                    raise UserError(_("Stages cannot be added to a closed or cancelled project."))
                vals['custom'] = True
                vals.pop('state', None)
        lines = super().create(vals_list)
        lines.mapped('project_id')._otm_recompute_stage_dates()
        return lines

    def write(self, vals):
        workflow = self.env.su or self.env.context.get('otm_transition')
        if not workflow:
            if 'state' in vals:
                raise UserError(_("The stage status cannot be edited directly. Use the workflow buttons."))
            if {'actual_start', 'actual_end', 'template_id', 'project_id'} & vals.keys():
                raise UserError(_("Actual dates are recorded automatically."))
            for line in self:
                line._otm_project_head_check()
        if {'planned_start', 'planned_deadline'} & vals.keys() \
                and not self.env.context.get('otm_recompute'):
            vals = dict(vals, date_override=True)
        old = {l.id: (l.planned_start, l.planned_deadline) for l in self} \
            if {'planned_start', 'planned_deadline'} & vals.keys() else {}
        res = super().write(vals)
        for line in self.filtered(lambda l: l.id in old):
            if (line.planned_start, line.planned_deadline) != old[line.id] and line.project_id:
                line.project_id.sudo().message_post(body=_(
                    "Stage '%(s)s' dates changed: %(o1)s → %(n1)s, deadline %(o2)s → %(n2)s",
                    s=line.name, o1=old[line.id][0] or '-', n1=line.planned_start or '-',
                    o2=old[line.id][1] or '-', n2=line.planned_deadline or '-'))
        if {'duration_days', 'sequence', 'date_override', 'planned_start', 'planned_deadline'} & vals.keys() \
                and not self.env.context.get('otm_recompute'):
            self.mapped('project_id')._otm_recompute_stage_dates()
        return res

    def unlink(self):
        if not self.env.su:
            for line in self:
                line._otm_project_head_check()
                if line.required and not line.custom:
                    raise UserError(_("The required stage '%s' cannot be deleted.", line.name))
                if line.state != 'pending':
                    raise UserError(_("Only a pending stage can be deleted."))
        projects = self.mapped('project_id')
        res = super().unlink()
        projects._otm_recompute_stage_dates()
        return res

    # -- transition engine ------------------------------------------------
    def _otm_log_scope(self):
        return (self.project_id.otm_sales_team_id, self.project_id.otm_sales_head_id)

    def _otm_check_actor(self, action):
        self.ensure_one()
        self._otm_project_head_check()

    def _otm_prerequisites(self, action):
        self.ensure_one()
        missing = []
        project = self.project_id
        if action in ('start', 'complete') and project.otm_state != 'in_progress':
            missing.append(_("The project must be in progress."))
        if action == 'start':
            earlier = project.otm_stage_ids.filtered(
                lambda l: l.sequence < self.sequence or (l.sequence == self.sequence and l.id < self.id))
            for l in earlier.filtered(lambda l: l.required and l.state not in ('done', 'skipped')):
                missing.append(_("The required stage '%s' is not finished.", l.name))
            if project.otm_stage_ids.filtered(lambda l: l.state == 'in_progress' and l != self):
                missing.append(_("Another stage is already in progress."))
        if action == 'skip' and self.required:
            missing.append(_("A required stage cannot be skipped."))
        if action == 'reopen' and project.otm_state in ('closed', 'cancelled'):
            missing.append(_("The project is closed."))
        return missing

    def _otm_after_transition(self, action, old_state, reason):
        now = fields.Datetime.now()
        for line in self:
            w = line.with_context(otm_transition=True)
            if action == 'start':
                w.write({'actual_start': now})
            elif action == 'complete':
                w.write({'actual_end': now})
            elif action == 'reopen':
                w.write({'actual_end': False})

    def _otm_require_reason(self, reason):
        if not (reason or '').strip():
            raise UserError(_("A reason is required for this action."))

    def action_start(self):
        return self._otm_do_transition('start')

    def action_complete(self):
        return self._otm_do_transition('complete')

    def action_skip(self, reason=None):
        self._otm_require_reason(reason)
        return self._otm_do_transition('skip', reason=reason)

    def action_reopen(self, reason=None):
        self._otm_require_reason(reason)
        return self._otm_do_transition('reopen', reason=reason)
