from odoo import _, api, fields, models
from odoo.exceptions import AccessError, UserError, ValidationError

ADMIN = 'sales_project_lifecycle.group_lifecycle_admin'
TASK_MATRIX = {
    'start': {'from': ('not_started',), 'to': 'in_progress'},
    'submit': {'from': ('in_progress',), 'to': 'submitted'},
    'return': {'from': ('submitted',), 'to': 'in_progress'},
    'complete': {'from': ('submitted',), 'to': 'completed'},
    'reopen': {'from': ('completed',), 'to': 'in_progress'},
}
DEV_FIELDS = {'otm_progress', 'otm_technical_notes', 'message_main_attachment_id', 'description'}


class ProjectTask(models.Model):
    _name = 'project.task'
    _inherit = ['project.task', 'otm.transition.mixin']
    _otm_state_field = 'otm_dev_status'
    _otm_matrix = TASK_MATRIX
    _otm_reason_methods = ('action_return', 'action_reopen')

    otm_lifecycle = fields.Boolean(related='project_id.otm_is_lifecycle', store=True, index=True)
    otm_dev_status = fields.Selection([
        ('not_started', 'Not Started'), ('in_progress', 'In Progress'),
        ('submitted', 'Submitted'), ('completed', 'Completed'),
    ], string='Development Status', default='not_started', copy=False, tracking=True, index=True)
    otm_stage_line_id = fields.Many2one(
        'otm.project.stage.line', string='Lifecycle Stage',
        domain="[('project_id', '=', project_id)]")
    otm_acceptance_criteria = fields.Text(string='Acceptance Criteria')
    otm_technical_notes = fields.Text(string='Technical Notes')
    otm_progress = fields.Integer(string='Progress (%)')
    # read-only information for the developer, always from the approved project
    otm_scope = fields.Text(related='project_id.otm_scope', string='Approved Scope')
    otm_requirements = fields.Text(related='project_id.otm_requirements', string='Customer Requirements')
    otm_customizations = fields.Text(related='project_id.otm_customizations', string='Approved Customizations')
    otm_technical_requirements = fields.Text(
        related='project_id.otm_technical_requirements', string='Project Technical Requirements',
        groups='sales_project_lifecycle.group_developer,sales_project_lifecycle.group_qc,'
               'sales_project_lifecycle.group_project_head')

    @api.constrains('otm_progress')
    def _check_progress(self):
        for task in self:
            if not 0 <= task.otm_progress <= 100:
                raise ValidationError(_("Progress must be between 0 and 100."))

    @api.constrains('user_ids', 'project_id')
    def _check_lifecycle_assignees(self):
        for task in self.sudo().filtered('otm_lifecycle'):
            allowed = task.project_id.otm_developer_ids | task.project_id.user_id \
                | task.project_id.otm_qc_user_id | task.project_id.otm_deploy_user_id \
                | task.project_id.otm_trainer_id
            for user in task.user_ids - allowed:
                raise ValidationError(_(
                    "%(user)s is not part of the project team of %(project)s.",
                    user=user.name, project=task.project_id.name))

    # ------------------------------------------------------------------
    def _otm_is_head(self, user=None):
        user = user or self.env.user
        return self.env.su or user.has_group(ADMIN) or user == self.sudo().project_id.user_id

    @api.model_create_multi
    def create(self, vals_list):
        if not self.env.su:
            for vals in vals_list:
                project = self.env['project.project'].sudo().browse(vals.get('project_id'))
                if project.otm_is_lifecycle:
                    user = self.env.user
                    if not (user.has_group(ADMIN) or user == project.user_id):
                        raise AccessError(_("Only the Project Head can create tasks on this project."))
                    if project.otm_state in ('closed', 'cancelled'):
                        raise UserError(_("Tasks cannot be added to a closed or cancelled project."))
                if 'otm_dev_status' in vals and vals['otm_dev_status'] != 'not_started':
                    raise UserError(_("A new task must start as 'Not Started'."))
        return super().create(vals_list)

    def write(self, vals):
        life = self.filtered('otm_lifecycle')
        if life and not (self.env.su or self.env.context.get('otm_transition')):
            if 'otm_dev_status' in vals:
                raise UserError(_("The development status cannot be edited directly. Use the workflow buttons."))
            user = self.env.user
            for task in life:
                if task._otm_is_head(user):
                    continue
                # developers: only their own assigned tasks, only a few fields
                if user not in task.user_ids:
                    raise AccessError(_("You can only update tasks assigned to you."))
                if set(vals) - DEV_FIELDS:
                    raise AccessError(_("Developers can only update progress, notes and the description."))
                if task.otm_dev_status in ('submitted', 'completed'):
                    raise UserError(_("The task is locked while it is submitted or completed."))
        return super().write(vals)

    def unlink(self):
        if self.filtered('otm_lifecycle') and not self._otm_is_head():
            raise AccessError(_("Only the Project Head can delete tasks."))
        return super().unlink()

    # -- transition engine --------------------------------------------------
    def _otm_log_scope(self):
        p = self.project_id
        return (p.otm_sales_team_id, p.otm_sales_head_id)

    def _otm_check_actor(self, action):
        self.ensure_one()
        if not self.otm_lifecycle:
            return
        user = self.env.user
        if self._otm_is_head(user):
            return
        if action in ('start', 'submit') and user in self.user_ids:
            return
        raise AccessError(_(
            "Only the assigned developer can start or submit a task; the Project Head completes it."))

    def _otm_prerequisites(self, action):
        self.ensure_one()
        missing = []
        if action in ('start', 'submit', 'complete') and self.project_id.otm_state != 'in_progress':
            missing.append(_("The project is not in progress."))
        if action == 'start' and not self.user_ids:
            missing.append(_("A developer must be assigned."))
        if action == 'submit' and self.otm_progress < 100:
            missing.append(_("Set the progress to 100% before submitting."))
        return missing

    def _otm_require_reason(self, reason):
        if not (reason or '').strip():
            raise UserError(_("A reason is required for this action."))

    def action_otm_start(self):
        return self._otm_do_transition('start')

    def action_otm_submit(self):
        return self._otm_do_transition('submit')

    def action_return(self, reason=None):
        self._otm_require_reason(reason)
        return self._otm_do_transition('return', reason=reason)

    def _otm_after_transition(self, action, old_state, reason):
        if action == 'complete':
            for project in self.mapped('project_id').sudo():
                tasks = project.task_ids.filtered(lambda t: t.otm_lifecycle)
                if tasks and all(t.otm_dev_status == 'completed' for t in tasks):
                    project._otm_sync_stages('development')

    def action_otm_complete(self):
        return self._otm_do_transition('complete')

    def action_reopen(self, reason=None):
        self._otm_require_reason(reason)
        return self._otm_do_transition('reopen', reason=reason)
