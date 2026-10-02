from datetime import date

from odoo import fields
from odoo.exceptions import AccessError, UserError, ValidationError
from odoo.tests import tagged

from .common import LifecycleCommon


class ProjectFixture(LifecycleCommon):

    def setUp(self):
        super().setUp()
        G = self.env.ref
        Users = self.env['res.users'].with_context(no_reset_password=True)

        def mk(name, login, xmlid):
            return Users.create({'name': name, 'login': login,
                                 'group_ids': [(6, 0, [G(xmlid).id])]})
        self.ph = mk('Project Head', 'tst_ph', 'sales_project_lifecycle.group_project_head')
        self.ph2 = mk('Project Head 2', 'tst_ph2', 'sales_project_lifecycle.group_project_head')
        self.dev1 = self.developer
        self.dev2 = mk('Developer 2', 'tst_dev2', 'sales_project_lifecycle.group_developer')
        self.qc = mk('QC', 'tst_qc', 'sales_project_lifecycle.group_qc')
        self.fin = self.finance
        self.fin_mgr = mk('FinMgr', 'tst_finmgr', 'sales_project_lifecycle.group_finance_manager')
        self.customer = self.env['res.partner'].create({'name': 'Cust'})
        lead = self.lead = self.lead_a1
        self._lead_to_estimate(lead)
        lead.with_user(self.exec_a1).action_negotiate()
        est = self.env['otm.estimate'].with_user(self.exec_a1).create({
            'lead_id': lead.id, 'customer_id': self.customer.id,
            'line_ids': [(0, 0, {'service_id': self.service.id})]})
        est.line_ids.with_user(self.head_a).write({'additional_amount': 15000})
        est.with_user(self.exec_a1).action_submit()
        est.with_user(self.head_a).action_send()
        est.with_user(self.head_a).action_approve()
        act = est.with_user(self.head_a).action_lock_deal(reason='ok')
        self.deal = self.env['otm.deal'].browse(act['res_id'])

    def _ready(self):
        act = self.deal.with_user(self.exec_a1).action_create_agreement()
        ag = self.env['otm.customer.agreement'].browse(act['res_id'])
        u = self.exec_a1
        ag.with_user(u).action_generate()
        ag.with_user(u).action_send()
        ag.with_user(u).write({'customer_acceptance': 'ok'})
        ag.with_user(u).action_accept()
        ag.with_user(u).write({'signed_document': 'UERG', 'signed_date': fields.Date.today()})
        ag.with_user(u).action_sign()
        ag.with_user(self.head_a).action_complete()
        adv = ag.payment_ids.filtered(lambda p: p.trigger == 'advance')
        adv.with_user(self.fin).write({'payment_reference': 'U1', 'payment_method': 'bank', 'proof': 'UFJPT0Y=',
                                       'paid_date': fields.Date.today()})
        adv.with_user(self.fin).action_confirm()
        return ag

    def _project(self):
        self._ready()
        act = self.deal.with_user(self.head_a).action_create_project()
        return self.env['project.project'].browse(act['res_id'])

    def _plan(self, project, start=date(2026, 11, 2)):
        p = project.with_user(self.ph)
        p.write({'user_id': self.ph.id, 'otm_start_date': start,
                 'otm_developer_ids': [(6, 0, [self.dev1.id])],
                 'otm_qc_user_id': self.qc.id})
        return p

    def _task(self, project, assignee=None, **kw):
        vals = {'name': 'Build CRM', 'project_id': project.id,
                'user_ids': [(6, 0, [(assignee or self.dev1).id])],
                'otm_acceptance_criteria': 'Leads CRUD works'}
        vals.update(kw)
        return self.env['project.task'].with_user(self.ph).create(vals)


@tagged('post_install', '-at_install', 'otm_project')
class TestProject(ProjectFixture):

    # -- creation / gate -----------------------------------------------------
    def test_gate_blocks_project_creation(self):
        act = self.deal.with_user(self.exec_a1).action_create_agreement()
        with self.assertRaises(UserError) as cm:
            self.deal.with_user(self.head_a).action_create_project()
        self.assertIn('Project cannot start until', str(cm.exception))
        self.assertTrue(act)

    def test_creation_roles_and_content(self):
        self._ready()
        for user in (self.exec_a1, self.head_b, self.finance):
            with self.assertRaises(AccessError):
                self.deal.with_user(user).action_create_project()
        project = self.env['project.project'].browse(
            self.deal.with_user(self.head_a).action_create_project()['res_id'])
        self.assertTrue(project.otm_is_lifecycle)
        self.assertEqual(project.otm_state, 'planning')
        self.assertEqual(project.privacy_visibility, 'followers')
        self.assertEqual(project.partner_id, self.customer)
        self.assertEqual(project.otm_sales_team_id, self.team_a)
        self.assertEqual(project.otm_sales_head_id, self.head_a)
        self.assertEqual(project.otm_salesperson_id, self.exec_a1)
        self.assertIn('Test CRM', project.otm_services)
        self.assertEqual(project.otm_requirements, 'Need CRM')
        self.assertEqual(len(project.otm_stage_ids), 12)
        self.assertEqual(project.otm_stage_ids[0].name, 'Requirement Analysis')
        self.assertEqual(self.lead.stage, 'project')
        self.assertEqual(self.deal.project_id, project)
        with self.assertRaises(UserError):
            self.deal.with_user(self.head_a).action_create_project()

    def test_protected_fields(self):
        project = self._project()
        for user in (self.admin_user, self.ph):
            for vals in ({'otm_scope': 'x'}, {'otm_state': 'closed'}, {'otm_deal_id': False},
                         {'otm_is_lifecycle': False}):
                with self.assertRaises(UserError):
                    project.with_user(user).write(vals)
        with self.assertRaises(UserError):
            project.with_user(self.admin_user).unlink()

    # -- team / start -----------------------------------------------------------
    def test_team_edit_rights(self):
        project = self._project()
        with self.assertRaises(AccessError):
            project.with_user(self.head_a).write({'user_id': self.ph.id})  # sales: read only
        project.with_user(self.ph).write({'user_id': self.ph.id})
        with self.assertRaises(AccessError):  # another project head cannot take over
            project.with_user(self.ph2).write({'otm_qc_user_id': self.qc.id})
        project.with_user(self.admin_user).write({'otm_qc_user_id': self.qc.id})
        with self.assertRaises(ValidationError):
            project.with_user(self.ph).write({'otm_developer_ids': [(6, 0, [self.exec_a1.id])]})

    def test_start_prerequisites(self):
        project = self._project()
        project.with_user(self.ph).write({'user_id': self.ph.id})
        with self.assertRaises(UserError) as cm:
            project.with_user(self.ph).action_otm_start()
        msg = str(cm.exception)
        self.assertIn('start date', msg)
        self.assertIn('developer', msg)
        self._plan(project)
        with self.assertRaises(AccessError):
            project.with_user(self.dev1).action_otm_start()
        project.with_user(self.ph).action_otm_start()
        self.assertEqual(project.otm_state, 'in_progress')
        self.assertEqual(project.otm_current_stage_id.name, 'Requirement Analysis')
        self.assertEqual(project.otm_current_stage_id.state, 'in_progress')

    def test_project_start_rechecks_gate(self):
        project = self._project()
        self._plan(project)
        self.deal.sudo().payment_ids.filtered(
            lambda p: p.trigger == 'advance').write({'status': 'pending'})
        self.deal._otm_refresh_payment_totals()
        with self.assertRaises(UserError) as cm:
            project.with_user(self.ph).action_otm_start()
        self.assertIn('Project cannot start until', str(cm.exception))

    def test_hold_resume_cancel(self):
        project = self._project()
        self._plan(project).action_otm_start()
        with self.assertRaises(UserError):
            project.with_user(self.ph).action_hold()
        project.with_user(self.ph).action_hold(reason='Customer delay')
        self.assertEqual(project.otm_state, 'on_hold')
        project.with_user(self.ph).action_otm_resume()
        project.with_user(self.ph).action_cancel(reason='Client left')
        self.assertEqual(project.otm_state, 'cancelled')
        with self.assertRaises(UserError):
            project.with_user(self.ph).write({'otm_qc_user_id': False})

    # -- dates / stages ------------------------------------------------------------
    def test_automatic_dates_and_override(self):
        project = self._project()
        self._plan(project)
        s = project.otm_stage_ids.sorted('sequence')
        self.assertEqual((s[0].planned_start, s[0].planned_deadline), (date(2026, 11, 2), date(2026, 11, 4)))
        self.assertEqual((s[1].planned_start, s[1].planned_deadline), (date(2026, 11, 4), date(2026, 11, 11)))
        # Project Head overrides the Development dates; later stages follow
        s[1].with_user(self.ph).write({'planned_deadline': date(2026, 11, 20)})
        self.assertTrue(s[1].date_override)
        self.assertEqual(s[2].planned_start, date(2026, 11, 20))
        # moving the project start only moves non-overridden stages
        project.with_user(self.ph).write({'otm_start_date': date(2026, 12, 1)})
        self.assertEqual(s[0].planned_start, date(2026, 12, 1))
        self.assertEqual(s[1].planned_deadline, date(2026, 11, 20))
        # changing a duration recalculates the following stages
        s[0].with_user(self.ph).write({'duration_days': 5})
        self.assertEqual(s[0].planned_deadline, date(2026, 12, 6))

    def test_stage_workflow_and_delay(self):
        project = self._project()
        self._plan(project).action_otm_start()
        s = project.otm_stage_ids.sorted('sequence')
        with self.assertRaises(UserError) as cm:
            s[2].with_user(self.ph).action_start()
        self.assertIn('not finished', str(cm.exception))
        with self.assertRaises(UserError):
            s[1].with_user(self.ph).action_start()  # another stage still in progress
        for user in (self.dev1, self.head_a, self.exec_a1):
            with self.assertRaises(AccessError):
                s[0].with_user(user).action_complete()
        with self.assertRaises(UserError):
            s[0].with_user(self.admin_user).write({'state': 'done'})
        with self.assertRaises(UserError):
            s[1].with_user(self.ph).action_skip(reason='x')  # required
        s[0].with_user(self.ph).action_complete()
        self.assertEqual(s[0].state, 'done')
        self.assertEqual(project.otm_progress, round(1 / 12 * 100, 5) if False else project.otm_progress)
        self.assertAlmostEqual(project.otm_progress, 100.0 / 12)
        s[1].with_user(self.ph).action_start()
        s[1].sudo().planned_deadline = date(2020, 1, 1)
        self.assertGreater(s[1].delay_days, 0)
        self.assertGreater(project.otm_delay_days, 0)
        logs = self.env['otm.transition.log'].search([('res_model', '=', 'otm.project.stage.line'),
                                                      ('res_id', 'in', project.otm_stage_ids.ids)])
        self.assertEqual(len(logs), 3)

    def test_custom_stage(self):
        project = self._project()
        self._plan(project)
        Line = self.env['otm.project.stage.line']
        with self.assertRaises(AccessError):
            Line.with_user(self.ph2).create({'project_id': project.id, 'name': 'X', 'sequence': 55})
        with self.assertRaises(AccessError):
            Line.with_user(self.head_a).create({'project_id': project.id, 'name': 'X'})
        custom = Line.with_user(self.ph).create({
            'project_id': project.id, 'name': 'Data migration', 'sequence': 25, 'duration_days': 3})
        self.assertTrue(custom.custom)
        self.assertTrue(custom.planned_start)
        custom.with_user(self.ph).unlink()
        with self.assertRaises(UserError):
            project.otm_stage_ids.filtered(lambda l: l.name == 'QC').with_user(self.ph).unlink()

    # -- tasks / developers ------------------------------------------------------------
    def test_task_creation_and_assignee_rules(self):
        project = self._project()
        self._plan(project)
        for user in (self.dev1, self.ph2):
            with self.assertRaises(AccessError):
                self.env['project.task'].with_user(user).create({
                    'name': 'x', 'project_id': project.id})
        with self.assertRaises(ValidationError):
            self._task(project, assignee=self.dev2)  # not part of the project team
        task = self._task(project)
        self.assertEqual(task.otm_dev_status, 'not_started')
        with self.assertRaises(UserError):
            task.with_user(self.ph).write({'otm_dev_status': 'completed'})

    def test_developer_visibility_and_secrets(self):
        project = self._project()
        self._plan(project)
        project.with_user(self.ph).write({'otm_developer_ids': [(6, 0, [self.dev1.id, self.dev2.id])]})
        t1 = self._task(project, self.dev1)
        t2 = self._task(project, self.dev2, name='Other task')
        Task = self.env['project.task']
        self.assertEqual(Task.with_user(self.dev1).search([('project_id', '=', project.id)]), t1)
        self.assertEqual(Task.with_user(self.dev2).search([('project_id', '=', project.id)]), t2)
        # the developer receives the approved information...
        data = t1.with_user(self.dev1).read(['otm_scope', 'otm_requirements', 'otm_acceptance_criteria'])[0]
        self.assertTrue(data['otm_scope'])
        self.assertEqual(data['otm_requirements'], 'Need CRM')
        # ...but never money, commission, wallet, discounts or the sales internals
        for model in ('otm.deal', 'otm.estimate', 'otm.sales.commission', 'otm.sales.wallet',
                      'otm.deal.payment', 'otm.customer.agreement', 'otm.lead'):
            with self.assertRaises(AccessError, msg=model):
                self.env[model].with_user(self.dev1).search([])
        with self.assertRaises(AccessError):
            project.with_user(self.dev1).read(['otm_deal_id'])
        with self.assertRaises(AccessError):
            project.with_user(self.dev1).read(['otm_estimate_id'])
        # only their own projects
        other_project = self.env['project.project'].sudo().create({'name': 'Other', 'otm_is_lifecycle': True,
                                                                    'privacy_visibility': 'followers'})
        self.assertFalse(self.env['project.project'].with_user(self.dev1).search(
            [('id', '=', other_project.id)]))
        self.assertTrue(self.env['project.project'].with_user(self.dev1).search(
            [('id', '=', project.id)]))

    def test_task_workflow(self):
        project = self._project()
        self._plan(project)
        task = self._task(project)
        with self.assertRaises(UserError):  # project not started yet
            task.with_user(self.dev1).action_otm_start()
        project.with_user(self.ph).action_otm_start()
        for user in (self.dev2, self.exec_a1):
            with self.assertRaises(AccessError):
                task.with_user(user).action_otm_start()
        task.with_user(self.dev1).action_otm_start()
        with self.assertRaises(UserError) as cm:
            task.with_user(self.dev1).action_otm_submit()
        self.assertIn('100%', str(cm.exception))
        task.with_user(self.dev1).write({'otm_progress': 100, 'otm_technical_notes': 'done'})
        with self.assertRaises(AccessError):
            task.with_user(self.dev1).write({'name': 'renamed'})
        with self.assertRaises(AccessError):
            task.with_user(self.dev1).write({'user_ids': [(6, 0, [])]})
        with self.assertRaises(UserError):
            task.with_user(self.dev1).write({'otm_dev_status': 'completed'})
        task.with_user(self.dev1).action_otm_submit()
        with self.assertRaises(UserError):  # locked while submitted
            task.with_user(self.dev1).write({'otm_progress': 50})
        with self.assertRaises(AccessError):  # developer cannot accept own work
            task.with_user(self.dev1).action_otm_complete()
        with self.assertRaises(UserError):
            task.with_user(self.ph).action_return()
        task.with_user(self.ph).action_return(reason='Missing validation')
        self.assertEqual(task.otm_dev_status, 'in_progress')
        task.with_user(self.dev1).action_otm_submit()
        task.with_user(self.ph).action_otm_complete()
        self.assertEqual(task.otm_dev_status, 'completed')
        with self.assertRaises(UserError):
            task.with_user(self.ph).action_otm_start()
        # a developer can never close or cancel the project
        with self.assertRaises(AccessError):
            project.with_user(self.dev1).action_cancel(reason='x')
        with self.assertRaises(Exception):
            project.with_user(self.dev1).write({'otm_state': 'closed'})

    # -- visibility of projects --------------------------------------------------------
    def test_project_isolation(self):
        project = self._project()
        P = self.env['project.project']
        for user in (self.exec_a1, self.head_a, self.fin, self.admin_user):
            self.assertTrue(P.with_user(user).search([('id', '=', project.id)]), user.name)
        for user in (self.exec_a2, self.head_b, self.exec_b1, self.dev1, self.qc):
            self.assertFalse(P.with_user(user).search([('id', '=', project.id)]), user.name)
        self._plan(project)
        self.assertTrue(P.with_user(self.qc).search([('id', '=', project.id)]))
        self.assertTrue(P.with_user(self.dev1).search([('id', '=', project.id)]))
        # sales can see the project status but cannot change it
        with self.assertRaises(AccessError):
            project.with_user(self.head_a).write({'name': 'renamed'})
        with self.assertRaises(AccessError):
            project.with_user(self.exec_a1).action_otm_start()

    def test_views_load_for_roles(self):
        project = self._project()
        self._plan(project)
        for user in (self.exec_a1, self.head_a, self.dev1, self.ph, self.fin):
            self.env['project.project'].with_user(user).get_views([(False, 'list'), (False, 'search')])
            project.with_user(user).get_views(
                [(self.env.ref('sales_project_lifecycle.view_otm_project_form').id, 'form')])
        self.env['project.task'].with_user(self.dev1).get_views([(False, 'form')])
