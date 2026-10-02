from datetime import date

from odoo import fields
from odoo.exceptions import AccessError, UserError, ValidationError
from odoo.tests import tagged

from .test_project import ProjectFixture


@tagged('post_install', '-at_install', 'otm_qc')
class TestQcDeployment(ProjectFixture):

    def setUp(self):
        super().setUp()
        G = self.env.ref
        self.deployer = self.env['res.users'].with_context(no_reset_password=True).create({
            'name': 'Deployer', 'login': 'tst_deployer',
            'group_ids': [(6, 0, [G('sales_project_lifecycle.group_developer').id])]})
        self.project = self._project()
        self.project.with_user(self.ph).write({
            'user_id': self.ph.id, 'otm_start_date': date(2026, 11, 2),
            'otm_developer_ids': [(6, 0, [self.dev1.id, self.dev2.id])],
            'otm_qc_user_id': self.qc.id, 'otm_deploy_user_id': self.deployer.id})
        self.project.with_user(self.ph).action_otm_start()
        self.server = self.env['otm.client.server'].with_user(self.ph).create({
            'name': 'Prod VPS', 'customer_id': self.customer.id, 'ip_address': '10.0.0.1',
            'credential_reference': 'vault://cust/prod'})

    def _dev_done(self, project=None):
        project = project or self.project
        t = self._task(project)
        t.with_user(self.dev1).action_otm_start()
        t.with_user(self.dev1).write({'otm_progress': 100})
        t.with_user(self.dev1).action_otm_submit()
        t.with_user(self.ph).action_otm_complete()
        return t

    def _round(self):
        act = self.project.with_user(self.ph).action_submit_qc()
        return self.env['otm.qc'].browse(act['res_id'])

    def _issue(self, qc, **kw):
        vals = {'title': 'Bug', 'project_id': self.project.id, 'qc_id': qc.id}
        vals.update(kw)
        return self.env['otm.qc.issue'].with_user(self.qc).create(vals)

    def _fixed_issue(self, qc):
        i = self._issue(qc)
        i.with_user(self.ph).write({'assigned_developer_id': self.dev1.id})
        i.with_user(self.ph).action_assign()
        i.with_user(self.dev1).write({'resolution_notes': 'fixed it'})
        i.with_user(self.dev1).action_fix()
        return i

    def _passed_qc(self):
        self._dev_done()
        qc = self._round()
        qc.with_user(self.qc).action_start()
        qc.with_user(self.qc).action_pass()
        return qc

    # -- submit for QC ------------------------------------------------------
    def test_submit_prerequisites(self):
        with self.assertRaises(UserError) as cm:
            self.project.with_user(self.ph).action_submit_qc()
        self.assertIn('no development tasks', str(cm.exception))
        t = self._task(self.project)
        with self.assertRaises(UserError) as cm:
            self.project.with_user(self.ph).action_submit_qc()
        self.assertIn('not completed', str(cm.exception))
        t.with_user(self.dev1).action_otm_start()
        t.with_user(self.dev1).write({'otm_progress': 100})
        t.with_user(self.dev1).action_otm_submit()
        t.with_user(self.ph).action_otm_complete()
        for u in (self.dev1, self.qc, self.ph2, self.head_a):
            with self.assertRaises(AccessError):
                self.project.with_user(u).action_submit_qc()
        qc = self._round()
        self.assertEqual((qc.status, qc.round_number), ('pending', 1))
        with self.assertRaises(UserError) as cm:
            self.project.with_user(self.ph).action_submit_qc()
        self.assertIn('still running', str(cm.exception))
        with self.assertRaises(UserError):
            self.env['otm.qc'].with_user(self.ph).create({'project_id': self.project.id})

    # -- QC round + issues -----------------------------------------------------
    def test_qc_pass_flow(self):
        self._dev_done()
        qc = self._round()
        for u in (self.dev1, self.ph, self.deployer):
            with self.assertRaises(AccessError):
                qc.with_user(u).action_start()
        qc.with_user(self.qc).action_start()
        with self.assertRaises(UserError):
            qc.with_user(self.qc).action_start()
        with self.assertRaises(UserError):
            qc.with_user(self.admin_user).write({'status': 'passed'})
        qc.with_user(self.qc).action_pass()
        self.assertEqual(qc.status, 'passed')
        self.assertEqual(self.project.otm_qc_state, 'passed')
        with self.assertRaises(UserError):
            qc.with_user(self.qc).action_fail()

    def test_fail_needs_issue_and_pass_blocked_by_issue(self):
        self._dev_done()
        qc = self._round()
        qc.with_user(self.qc).action_start()
        with self.assertRaises(UserError):
            qc.with_user(self.qc).action_fail()
        i = self._issue(qc)
        with self.assertRaises(UserError) as cm:
            qc.with_user(self.qc).action_pass()
        self.assertIn('still Open', str(cm.exception))
        qc.with_user(self.qc).action_fail()
        self.assertEqual(qc.status, 'failed')
        self.assertEqual(i.status, 'open')

    def test_full_fail_fix_retest_cycle(self):
        self._dev_done()
        qc1 = self._round()
        qc1.with_user(self.qc).action_start()
        issue = self._issue(qc1, severity='critical')
        qc1.with_user(self.qc).action_fail()
        # cannot resubmit before the issue is fixed
        with self.assertRaises(UserError) as cm:
            self.project.with_user(self.ph).action_submit_qc()
        self.assertIn('not been fixed', str(cm.exception))
        with self.assertRaises(UserError):
            issue.with_user(self.ph).action_assign()  # developer missing
        with self.assertRaises(ValidationError):
            issue.with_user(self.ph).write({'assigned_developer_id': self.exec_a1.id})
        issue.with_user(self.ph).write({'assigned_developer_id': self.dev1.id})
        with self.assertRaises(AccessError):
            issue.with_user(self.qc).action_assign()
        issue.with_user(self.ph).action_assign()
        with self.assertRaises(UserError):
            issue.with_user(self.dev1).action_fix()  # notes required
        with self.assertRaises(AccessError):
            issue.with_user(self.dev1).write({'title': 'edit'})
        with self.assertRaises(AccessError):
            issue.with_user(self.dev2).action_fix()
        issue.with_user(self.dev1).write({'resolution_notes': 'patched'})
        issue.with_user(self.dev1).action_fix()
        self.assertEqual(issue.status, 'fixed')
        with self.assertRaises(UserError):
            issue.with_user(self.qc).action_pass()  # not in retest yet
        qc2 = self._round()
        self.assertEqual(qc2.round_number, 2)
        self.assertEqual(issue.status, 'retest')
        qc2.with_user(self.qc).action_start()
        issue.with_user(self.qc).action_fail(reason='still broken')
        self.assertEqual(issue.status, 'assigned')
        qc2.with_user(self.qc).action_fail()  # failed retest keeps the issue unresolved
        self.assertEqual(qc2.status, 'failed')
        issue.with_user(self.dev1).write({'resolution_notes': 'again'})
        issue.with_user(self.dev1).action_fix()
        self.assertEqual(issue.status, 'fixed')
        qc3 = self._round()
        self.assertEqual((qc3.round_number, issue.status), (3, 'retest'))

    def test_retest_pass_then_qc_pass(self):
        self._dev_done()
        qc1 = self._round()
        qc1.with_user(self.qc).action_start()
        issue = self._issue(qc1)
        qc1.with_user(self.qc).action_fail()
        issue.with_user(self.ph).write({'assigned_developer_id': self.dev1.id})
        issue.with_user(self.ph).action_assign()
        issue.with_user(self.dev1).write({'resolution_notes': 'ok'})
        issue.with_user(self.dev1).action_fix()
        qc2 = self._round()
        qc2.with_user(self.qc).action_start()
        issue.with_user(self.qc).action_pass()
        self.assertTrue(issue.resolved_date)
        qc2.with_user(self.qc).action_pass()
        self.assertEqual(self.project.otm_qc_state, 'passed')
        self.assertEqual(self.project.otm_open_issue_count, 0)
        # history kept
        logs = self.env['otm.transition.log'].search(
            [('res_model', '=', 'otm.qc.issue'), ('res_id', '=', issue.id)], order='id')
        self.assertEqual([l.to_state for l in logs],
                         ['Assigned', 'Fixed', 'Retest', 'Passed'])

    def test_reject_issue_and_creation_rules(self):
        self._dev_done()
        qc = self._round()
        i = self._issue(qc)
        for u in (self.dev1, self.exec_a1, self.ph2):
            with self.assertRaises(AccessError):
                self.env['otm.qc.issue'].with_user(u).create({'title': 'x', 'project_id': self.project.id})
        with self.assertRaises(UserError):
            i.with_user(self.qc).action_reject()
        i.with_user(self.qc).action_reject(reason='Not a bug')
        self.assertEqual(i.status, 'rejected')
        with self.assertRaises(UserError):
            i.with_user(self.qc).write({'title': 'late'})
        with self.assertRaises(UserError):
            i.with_user(self.admin_user).write({'status': 'open'})
        with self.assertRaises(UserError):
            i.with_user(self.admin_user).unlink()

    def test_issue_visibility(self):
        self._dev_done()
        qc = self._round()
        i = self._issue(qc)
        i.with_user(self.ph).write({'assigned_developer_id': self.dev1.id})
        I = self.env['otm.qc.issue']
        for u in (self.qc, self.ph, self.admin_user, self.dev1):
            self.assertTrue(I.with_user(u).search([('id', '=', i.id)]), u.name)
        for u in (self.dev2, self.deployer):
            self.assertFalse(I.with_user(u).search([('id', '=', i.id)]), u.name)
        for u in (self.exec_a1, self.head_a, self.fin):  # sales never see QC internals
            for model in ('otm.qc', 'otm.qc.issue', 'otm.deployment', 'otm.client.server'):
                with self.assertRaises(AccessError, msg=f'{u.name} {model}'):
                    self.env[model].with_user(u).search([])

    # -- deployment -----------------------------------------------------------------
    def _deployment(self, **kw):
        vals = {'project_id': self.project.id, 'server_id': self.server.id, 'version': '1.0.0',
                'backup_confirmed': True, 'rollback_available': True}
        vals.update(kw)
        return self.env['otm.deployment'].with_user(self.ph).create(vals)

    def test_deployment_requires_qc_pass(self):
        dep = self._deployment()
        with self.assertRaises(UserError) as cm:
            dep.with_user(self.ph).action_approve()
        self.assertIn('QC round must be passed', str(cm.exception))
        self._passed_qc()
        dep.with_user(self.ph).write({'backup_confirmed': False})
        with self.assertRaises(UserError) as cm:
            dep.with_user(self.ph).action_approve()
        self.assertIn('backup', str(cm.exception))
        dep.with_user(self.ph).write({'backup_confirmed': True})
        for u in (self.deployer, self.dev1, self.ph2):
            with self.assertRaises(AccessError):
                dep.with_user(u).action_approve()
        dep.with_user(self.ph).action_approve()
        self.assertEqual(dep.approved_by_id, self.ph)

    def test_deployment_happy_path_with_verification(self):
        self._passed_qc()
        dep = self._deployment()
        with self.assertRaises(UserError):
            self._deployment()  # one active deployment per project
        with self.assertRaises(UserError):
            dep.with_user(self.deployer).action_deploy()  # not approved
        dep.with_user(self.ph).action_approve()
        with self.assertRaises(AccessError):
            dep.with_user(self.dev1).action_deploy()
        dep.with_user(self.deployer).action_deploy()
        self.assertEqual(dep.deployed_by_id, self.deployer)
        self.assertTrue(dep.deployment_date)
        with self.assertRaises(UserError):
            dep.with_user(self.deployer).write({'version': '9'})  # locked
        with self.assertRaises(UserError):
            dep.with_user(self.deployer).write({'customer_confirmation': True})  # too early
        dep.with_user(self.deployer).action_verify()
        with self.assertRaises(UserError) as cm:
            dep.with_user(self.deployer).action_complete()
        self.assertIn('not confirmed', str(cm.exception))
        dep.with_user(self.deployer).write({
            'customer_confirmation': True, 'verified_by': 'Mr Customer',
            'verification_date': fields.Date.today()})
        dep.with_user(self.deployer).action_complete()
        self.assertEqual(dep.status, 'completed')
        with self.assertRaises(UserError):
            dep.with_user(self.ph).write({'deployment_notes': 'x'})
        with self.assertRaises(UserError):
            dep.with_user(self.admin_user).write({'status': 'pending'})

    def test_customer_issue_routes_back(self):
        self._passed_qc()
        dep = self._deployment()
        dep.with_user(self.ph).action_approve()
        dep.with_user(self.deployer).action_deploy()
        dep.with_user(self.deployer).action_verify()
        with self.assertRaises(UserError):
            dep.with_user(self.deployer).action_report_issue(reason='x')  # issue text missing
        dep.with_user(self.deployer).write({'verification_issues': 'Invoice print is broken'})
        dep.with_user(self.deployer).action_report_issue(reason='Customer reported')
        self.assertEqual(dep.status, 'issue')
        issue = self.project.otm_issue_ids
        self.assertEqual((issue.status, issue.source, issue.severity), ('open', 'customer', 'high'))
        # a new deployment needs a fresh passed QC
        dep2 = self._deployment(version='1.0.1')
        with self.assertRaises(UserError):
            dep2.with_user(self.ph).action_approve()
        # and the fix goes through developer -> QC again
        issue.with_user(self.ph).write({'assigned_developer_id': self.dev1.id})
        issue.with_user(self.ph).action_assign()
        issue.with_user(self.dev1).write({'resolution_notes': 'fixed print'})
        issue.with_user(self.dev1).action_fix()
        qc = self._round()
        qc.with_user(self.qc).action_start()
        issue.with_user(self.qc).action_pass()
        qc.with_user(self.qc).action_pass()
        dep2.with_user(self.ph).action_approve()

    def test_rollback_rules(self):
        self._passed_qc()
        dep = self._deployment(rollback_available=False)
        dep.with_user(self.ph).action_approve()
        dep.with_user(self.deployer).action_deploy()
        with self.assertRaises(UserError):
            dep.with_user(self.deployer).action_rollback()  # reason first
        with self.assertRaises(UserError) as cm:
            dep.with_user(self.deployer).action_rollback(reason='bad')
        self.assertIn('No rollback', str(cm.exception))
        dep.with_user(self.ph).write({'rollback_available': True})
        dep.with_user(self.deployer).action_rollback(reason='Outage')
        self.assertEqual(dep.status, 'rollback')

    def test_deployment_access(self):
        dep = self._deployment()
        D = self.env['otm.deployment']
        self.assertTrue(D.with_user(self.deployer).search([('id', '=', dep.id)]))
        self.assertTrue(D.with_user(self.ph).search([('id', '=', dep.id)]))
        self.assertFalse(D.with_user(self.dev1).search([('id', '=', dep.id)]))
        self.assertFalse(D.with_user(self.qc).search([('id', '=', dep.id)]))
        for u in (self.deployer, self.dev1):
            with self.assertRaises(AccessError):
                D.with_user(u).create({'project_id': self.project.id, 'version': '1'})
        S = self.env['otm.client.server']
        self.assertTrue(S.with_user(self.deployer).search([('id', '=', self.server.id)]))
        self.assertFalse(S.with_user(self.dev1).search([('id', '=', self.server.id)]))
        with self.assertRaises(AccessError):
            self.server.with_user(self.deployer).write({'ip_address': '1.1.1.1'})
        with self.assertRaises(ValidationError):
            self.server.with_user(self.ph).write({'credential_reference': 'password: abc123'})

    def test_views_load(self):
        dep = self._deployment()
        for u in (self.ph, self.qc, self.dev1, self.deployer):
            for model in ('otm.qc', 'otm.qc.issue', 'otm.deployment', 'otm.client.server'):
                try:
                    self.env[model].with_user(u).get_views([(False, 'form'), (False, 'list')])
                except AccessError:
                    pass
        self.project.with_user(self.ph).get_views(
            [(self.env.ref('sales_project_lifecycle.view_otm_project_form').id, 'form')])
        self.assertTrue(dep)
