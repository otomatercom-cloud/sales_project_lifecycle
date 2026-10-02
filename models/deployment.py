from odoo import _, api, fields, models
from odoo.exceptions import AccessError, UserError

ADMIN = 'sales_project_lifecycle.group_lifecycle_admin'
DEP_MATRIX = {
    'approve': {'from': ('pending',), 'to': 'approved'},
    'deploy': {'from': ('approved',), 'to': 'deployed'},
    'verify': {'from': ('deployed',), 'to': 'verification'},
    'complete': {'from': ('verification',), 'to': 'completed'},
    'report_issue': {'from': ('verification',), 'to': 'issue'},
    'rollback': {'from': ('deployed', 'verification'), 'to': 'rollback'},
}
VERIFY_FIELDS = {'verification_date', 'verified_by', 'customer_confirmation',
                 'verification_notes', 'verification_issues'}


class OtmDeployment(models.Model):
    _name = 'otm.deployment'
    _description = 'Deployment'
    _inherit = ['mail.thread', 'otm.transition.mixin']
    _order = 'id desc'
    _otm_state_field = 'status'
    _otm_matrix = DEP_MATRIX
    _otm_reason_methods = ('action_report_issue', 'action_rollback')

    name = fields.Char(string='Reference', readonly=True, copy=False, default=lambda s: _('New'))
    project_id = fields.Many2one('project.project', required=True, index=True, ondelete='restrict')
    customer_id = fields.Many2one('res.partner', related='project_id.partner_id', store=True)
    sales_team_id = fields.Many2one('otm.sales.team', related='project_id.otm_sales_team_id', store=True)
    sales_head_id = fields.Many2one('res.users', related='project_id.otm_sales_head_id', store=True)
    server_id = fields.Many2one('otm.client.server', string='Client Server', tracking=True,
                                domain="[('customer_id', '=', customer_id)]")
    environment = fields.Selection([
        ('staging', 'Staging / UAT'), ('production', 'Production')], default='production', required=True)
    version = fields.Char(required=True, tracking=True)
    deployment_date = fields.Datetime(readonly=True)
    deployed_by_id = fields.Many2one('res.users', readonly=True)
    approved_by_id = fields.Many2one('res.users', readonly=True)
    deployment_notes = fields.Text()
    backup_confirmed = fields.Boolean(tracking=True)
    rollback_available = fields.Boolean(tracking=True)
    status = fields.Selection([
        ('pending', 'Pending'), ('approved', 'Approved'), ('deployed', 'Deployed'),
        ('verification', 'Customer Verification'), ('completed', 'Completed'),
        ('issue', 'Customer Issue'), ('rollback', 'Rolled Back'),
    ], default='pending', required=True, tracking=True, index=True, copy=False)
    # customer verification
    verification_date = fields.Date()
    verified_by = fields.Char(string='Verified By (customer)')
    customer_confirmation = fields.Boolean(string='Customer Confirmed')
    verification_notes = fields.Text()
    verification_issues = fields.Text(string='Issues Reported')

    @api.model_create_multi
    def create(self, vals_list):
        seq = self.env['ir.sequence'].sudo()
        for vals in vals_list:
            vals['name'] = seq.next_by_code('otm.deployment') or _('New')
            if not self.env.su:
                project = self.env['project.project'].sudo().browse(vals.get('project_id'))
                user = self.env.user
                if not (user.has_group(ADMIN) or user == project.user_id):
                    raise AccessError(_("Only the Project Head can create a deployment."))
                if not self.env.context.get('otm_transition'):
                    vals.pop('status', None)
                if project.otm_state != 'in_progress':
                    raise UserError(_("The project is not in progress."))
                if self.sudo().search_count([('project_id', '=', project.id), (
                        'status', 'in', ('pending', 'approved', 'deployed', 'verification'))]):
                    raise UserError(_("This project already has an active deployment."))
        return super().create(vals_list)

    def write(self, vals):
        if not (self.env.su or self.env.context.get('otm_transition')):
            if 'status' in vals:
                raise UserError(_("The deployment status cannot be edited directly. Use the workflow buttons."))
            if {'project_id', 'deployment_date', 'deployed_by_id', 'approved_by_id'} & vals.keys():
                raise UserError(_("These fields are set automatically."))
            for dep in self:
                if dep.status in ('completed', 'issue', 'rollback'):
                    raise UserError(_("A finished deployment cannot be changed."))
                if VERIFY_FIELDS & vals.keys() and dep.status != 'verification':
                    raise UserError(_("Verification details are entered while the deployment is in Customer Verification."))
                if dep.status in ('deployed', 'verification') and {'server_id', 'version', 'environment'} & vals.keys():
                    raise UserError(_("The deployment target and version are locked after deployment."))
                dep._otm_check_team_member()
        return super().write(vals)

    def unlink(self):
        if not self.env.su:
            raise UserError(_("Deployments cannot be deleted."))
        return super().unlink()

    def _otm_check_team_member(self):
        user = self.env.user
        p = self.sudo().project_id
        if not (self.env.su or user.has_group(ADMIN) or user in (p.user_id | p.otm_deploy_user_id)):
            raise AccessError(_("Only the Project Head or the deployment responsible can do this."))

    def _otm_log_scope(self):
        return (self.sales_team_id, self.sales_head_id)

    def _otm_check_actor(self, action):
        self.ensure_one()
        user = self.env.user
        if self.env.su or user.has_group(ADMIN):
            return
        p = self.project_id.sudo()
        ok = user == p.user_id if action == 'approve' else user in (p.user_id | p.otm_deploy_user_id)
        if not ok:
            raise AccessError(_("You are not allowed to perform this action on this deployment."))

    def _otm_prerequisites(self, action):
        self.ensure_one()
        missing = []
        p = self.project_id.sudo()
        if action == 'approve':
            last = self.env['otm.qc'].sudo().search([('project_id', '=', p.id)], order='id desc', limit=1)
            if not last or last.status != 'passed':
                missing.append(_("The latest QC round must be passed."))
            if p.otm_issue_ids.filtered(lambda i: i.status in ('open', 'assigned', 'fixed', 'retest')):
                missing.append(_("There are unresolved QC issues."))
            if not self.server_id:
                missing.append(_("The client server is not selected."))
            if not self.backup_confirmed:
                missing.append(_("Confirm that a backup has been taken."))
            if not (self.version or '').strip():
                missing.append(_("The version is required."))
            if not p.otm_deploy_user_id:
                missing.append(_("Assign the deployment responsible on the project."))
        if action == 'deploy' and p.otm_state != 'in_progress':
            missing.append(_("The project is not in progress."))
        if action == 'complete':
            if not self.customer_confirmation:
                missing.append(_("The customer has not confirmed the deployment."))
            if not self.verified_by:
                missing.append(_("Record who verified on the customer side."))
            if not self.verification_date:
                missing.append(_("The verification date is required."))
        if action == 'report_issue' and not (self.verification_issues or '').strip():
            missing.append(_("Describe the issue reported by the customer."))
        if action == 'rollback' and not self.rollback_available:
            missing.append(_("No rollback is available for this deployment."))
        return missing

    def _otm_after_transition(self, action, old_state, reason):
        now = fields.Datetime.now()
        for dep in self:
            w = dep.with_context(otm_transition=True)
            if action == 'approve':
                w.write({'approved_by_id': self.env.user.id})
            elif action == 'deploy':
                w.write({'deployment_date': now, 'deployed_by_id': self.env.user.id})
                dep.project_id.sudo()._otm_sync_stages('deployment')
            elif action == 'complete':
                dep.project_id.sudo()._otm_sync_stages('verification')
            elif action == 'report_issue':
                p = dep.project_id.sudo()
                self.env['otm.qc.issue'].sudo().create({
                    'title': _("Customer issue on %s", dep.name),
                    'description': dep.verification_issues, 'project_id': p.id,
                    'severity': 'high', 'source': 'customer'})

    def _otm_require_reason(self, reason):
        if not (reason or '').strip():
            raise UserError(_("A reason is required for this action."))

    def action_approve(self):
        return self._otm_do_transition('approve')

    def action_deploy(self):
        return self._otm_do_transition('deploy')

    def action_verify(self):
        return self._otm_do_transition('verify')

    def action_complete(self):
        return self._otm_do_transition('complete')

    def action_report_issue(self, reason=None):
        self._otm_require_reason(reason)
        return self._otm_do_transition('report_issue', reason=reason)

    def action_rollback(self, reason=None):
        self._otm_require_reason(reason)
        return self._otm_do_transition('rollback', reason=reason)
