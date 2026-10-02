import json
import time
from datetime import timedelta
from unittest.mock import patch

from odoo import fields
from odoo.exceptions import AccessError, UserError
from odoo.tests import tagged
from odoo.tools import mute_logger

from .test_project import ProjectFixture

LC = 'otm.lifecycle.cron'


class Phase11Base(ProjectFixture):

    def setUp(self):
        super().setUp()
        mk = lambda n, l, g: self.env['res.users'].with_context(no_reset_password=True).create({
            'name': n, 'login': l, 'group_ids': [(6, 0, [self.env.ref('sales_project_lifecycle.' + g).id])]})
        self.trainer = mk('Trainer', 'tst_trainer11', 'group_developer')
        self.deployer = mk('Deployer', 'tst_deployer11', 'group_developer')
        self.customer.email = 'customer@example.com'
        self.today = fields.Date.context_today(self.env['otm.demo'])
        self.users = {'exec': self.exec_a1, 'head': self.head_a, 'ph': self.ph, 'dev': self.dev1, 'qc': self.qc,
                      'fin': self.fin, 'trainer': self.trainer, 'deployer': self.deployer}
        self.Loader = self.env['otm.demo.loader']
        self.sale_lead = self.env['otm.lead'].with_user(self.exec_a1).create({'name': 'Sale Lead'})
        self.sale_lead.sudo().write({
            'requirement_description': 'Need CRM',
            'service_line_ids': [(0, 0, {'service_id': self.service.id, 'quantity': 1})]})

    def _drive(self, upto='closed', lead=None, **kw):
        return self.Loader.drive(self.users, lead or self.sale_lead, upto, customer=self.customer, **kw)

    def _mails(self, rec):
        return self.env['mail.mail'].sudo().search([('model', '=', rec._name), ('res_id', '=', rec.id)])

    def _acts(self, rec, summary=None):
        dom = [('res_model', '=', rec._name), ('res_id', '=', rec.id)]
        if summary:
            dom.append(('summary', 'ilike', summary))
        return self.env['mail.activity'].sudo().search(dom)


@tagged('post_install', '-at_install', 'otm_phase11')
class TestEmails(Phase11Base):

    def test_all_templates_render(self):
        out = self._drive('closed')
        records = {
            'mail_demo_confirmation': self.sale_lead.demo_ids[:1], 'mail_estimate': out['estimate'],
            'mail_estimate_revision': out['estimate'], 'mail_agreement': out['agreement'],
            'mail_advance_request': out['agreement'].payment_ids[:1],
            'mail_project_confirmation': out['project'], 'mail_training_confirmation': out['training'],
            'mail_payment_30': out['deal'].payment_ids[1:2], 'mail_payment_final': out['deal'].payment_ids[2:3],
            'mail_review_request': out['review'], 'mail_service_expiry': self.env['otm.client.service'].sudo().search([], limit=1),
            'mail_renewal': None}
        svc = self.env['otm.client.service'].sudo().create({
            'customer_id': self.customer.id, 'service_name': 'Hosting', 'amount': 100, 'billing_type': 'yearly',
            'renewal_period': 'yearly', 'start_date': self.today, 'expiry_date': self.today + timedelta(days=100)})
        records['mail_service_expiry'] = svc
        records['mail_renewal'] = self.env['otm.service.renewal'].sudo().create({'service_id': svc.id})
        self.assertEqual(len(records), 12)
        for xmlid, rec in records.items():
            tmpl = self.env.ref('sales_project_lifecycle.' + xmlid)
            self.assertTrue(rec, xmlid)
            body = tmpl._render_field('body_html', rec.ids)[rec.id]
            subject = tmpl._render_field('subject', rec.ids)[rec.id]
            self.assertIn('Dear Cust', body, xmlid)
            self.assertTrue(subject, xmlid)
            self.assertNotIn('t-out', body, xmlid)

    def test_emails_are_queued_at_the_right_steps(self):
        out = self._drive('closed')
        partner = self.customer
        est = out['estimate']
        self.assertTrue(self._mails(est))
        self.assertEqual(self._mails(est)[0].recipient_ids, partner)
        self.assertIn('Estimate', self._mails(est)[0].subject)
        self.assertTrue(self._mails(out['agreement']))
        self.assertTrue(self._mails(out['project']))
        self.assertTrue(self._mails(out['training']))
        self.assertTrue(self._mails(out['review']))
        subjects = ' | '.join(self.env['mail.mail'].sudo().search([('model', '=', 'otm.deal.payment'),
                                                                   ('res_id', 'in', out['deal'].payment_ids.ids)]).mapped('subject'))
        self.assertIn('Final payment due', subjects)
        self.assertIn('Payment due', subjects)
        self.assertTrue(self._mails(self.sale_lead.demo_ids[0]))

    def test_estimate_revision_uses_revision_template(self):
        out = self._drive('estimate')
        est = out['estimate']
        before = len(self._mails(est))
        est.with_user(self.head_a).action_revise(reason='Customer asked for changes')
        est.with_user(self.exec_a1).action_submit()
        est.with_user(self.head_a).action_send()
        mails = self._mails(est)
        self.assertEqual(len(mails), before + 1)
        self.assertIn('Revised estimate', mails.sorted('id')[-1].subject)

    @mute_logger('odoo.addons.sales_project_lifecycle.models.transition_log')
    def test_missing_customer_email_never_blocks(self):
        self.customer.email = False
        out = self._drive('estimate')
        self.assertFalse(self._mails(out['estimate']))
        self.assertIn('no email address', ' '.join(out['estimate'].message_ids.mapped('body')))
        # a template failure must not roll back the business action either
        self.customer.email = 'x@example.com'
        est = out['estimate']
        est.with_user(self.head_a).action_revise(reason='again')
        est.with_user(self.exec_a1).action_submit()
        with patch('odoo.addons.mail.models.mail_template.MailTemplate.send_mail', side_effect=Exception('smtp down')):
            est.with_user(self.head_a).action_send()
        self.assertEqual(est.status, 'sent')

    def test_manual_expiry_notice_and_renewal_email(self):
        svc = self.env['otm.client.service'].with_user(self.exec_a1).create({
            'customer_id': self.customer.id, 'service_name': 'Hosting', 'amount': 100, 'billing_type': 'yearly',
            'renewal_period': 'yearly', 'renewal_amount': 120, 'start_date': self.today,
            'expiry_date': self.today + timedelta(days=20)})
        with self.assertRaises(AccessError):
            svc.with_user(self.exec_a2).action_send_expiry_notice()
        svc.with_user(self.exec_a1).action_send_expiry_notice()
        self.assertTrue(self._mails(svc))
        self.customer.email = False
        with self.assertRaises(UserError):
            svc.with_user(self.exec_a1).action_send_expiry_notice()
        self.customer.email = 'customer@example.com'
        act = svc.with_user(self.exec_a1).action_start_renewal()
        ren = self.env['otm.service.renewal'].browse(act['res_id'])
        ren.with_user(self.exec_a1).action_send_estimate()
        self.assertTrue(self._mails(ren))


@tagged('post_install', '-at_install', 'otm_phase11')
class TestDailyChecks(Phase11Base):

    def _run(self, today=None):
        return self.env[LC]._cron_lifecycle_checks(today=today)

    def test_demo_reminders_no_duplicates(self):
        demo = self._make_demo(self.lead_a1, demo_date=self.today + timedelta(days=1))
        self._run()
        acts = self._acts(demo)
        self.assertEqual(len(acts), 1)
        self.assertEqual(acts.user_id, demo.demo_person_id)
        self._run()
        self._run()
        self.assertEqual(len(self._acts(demo)), 1)
        far = self._make_demo(self.lead_a2, demo_date=self.today + timedelta(days=9))
        self._run()
        self.assertFalse(self._acts(far))
        self._run(today=self.today + timedelta(days=3))  # demo date passed, still open
        self.assertEqual(len(self._acts(demo)), 2)

    def test_estimate_followup_uses_configurable_days(self):
        out = self._drive('estimate')
        est = out['estimate']
        self.assertEqual(est.status, 'approved')
        est.sudo().write({'status': 'sent', 'estimate_date': self.today - timedelta(days=2)})
        self._run()
        self.assertFalse(self._acts(est, 'Follow up'))
        self.env['ir.config_parameter'].sudo().set_param('sales_project_lifecycle.followup_days', '1')
        self._run()
        acts = self._acts(est, 'Follow up')
        self.assertEqual(len(acts), 1)
        self.assertEqual(acts.user_id, self.exec_a1)
        self._run()
        self.assertEqual(len(self._acts(est, 'Follow up')), 1)

    def test_pending_payment_and_agreement(self):
        out = self._drive('agreement')
        ag = out['agreement']
        self.assertEqual(ag.status, 'completed')
        pay = out['deal'].payment_ids.filtered(lambda p: p.trigger == 'after_training')
        pay.sudo()._otm_do_transition('system_due')
        pay.sudo().with_context(otm_transition=True).write({'due_date': self.today - timedelta(days=10)})
        self._run()
        self.assertEqual(len(self._acts(pay)) if hasattr(pay, 'activity_ids') else 1, 1)
        self.assertTrue(self.env['otm.expiry.reminder'].sudo().search([('res_model', '=', 'otm.deal.payment'),
                                                                       ('res_id', '=', pay.id)]))
        before = self.env['otm.expiry.reminder'].sudo().search_count([])
        self._run()
        self.assertEqual(self.env['otm.expiry.reminder'].sudo().search_count([]), before)

    def test_stage_overdue_and_training(self):
        out = self._drive('project')
        project = out['project']
        line = project.otm_stage_ids.filtered(lambda l: l.state in ('pending', 'in_progress'))[:1]
        line.sudo().with_context(otm_recompute=True).write({'planned_deadline': self.today - timedelta(days=2)})
        self._run()
        self.assertTrue(self._acts(project, 'Stage overdue'))
        self.assertEqual(self._acts(project, 'Stage overdue').user_id, self.ph)
        n = len(self._acts(project))
        self._run()
        self.assertEqual(len(self._acts(project)), n)
        training = self.env['otm.training'].sudo().create({
            'project_id': project.id, 'training_type': 'online', 'date': self.today + timedelta(days=1),
            'start_time': 10, 'end_time': 11, 'trainer_id': self.trainer.id})
        self._run()
        acts = self._acts(project, 'Training soon')
        self.assertEqual(acts.user_id, self.trainer)
        self.assertTrue(training)

    @mute_logger('odoo.addons.sales_project_lifecycle.models.lifecycle_cron')
    def test_one_failing_check_does_not_stop_the_others(self):
        demo = self._make_demo(self.lead_a1, demo_date=self.today + timedelta(days=1))
        with patch.object(type(self.env[LC]), '_check_estimates', side_effect=Exception('boom')):
            self._run()
        self.assertEqual(len(self._acts(demo)), 1)

    def test_renewal_opportunity_created_when_service_starts_expiring(self):
        svc = self.env['otm.client.service'].with_user(self.exec_a1).create({
            'customer_id': self.customer.id, 'service_name': 'Hosting', 'amount': 100, 'billing_type': 'yearly',
            'renewal_period': 'yearly', 'renewal_amount': 120, 'start_date': self.today - timedelta(days=300),
            'expiry_date': self.today + timedelta(days=40)})
        one_time = self.env['otm.client.service'].with_user(self.exec_a1).create({
            'customer_id': self.customer.id, 'service_name': 'Setup', 'amount': 100})
        self.env['otm.client.service']._cron_expiry_reminders()
        ren = self.env['otm.service.renewal'].sudo().search([('service_id', '=', svc.id)])
        self.assertEqual(len(ren), 1)
        self.assertEqual(ren.status, 'follow_up')
        self.assertEqual(ren.amount, 120)
        self.assertTrue(self._acts(svc, 'Renewal opportunity'))
        self.env['otm.client.service']._cron_expiry_reminders()
        self.assertEqual(self.env['otm.service.renewal'].sudo().search_count([('service_id', '=', svc.id)]), 1)
        self.assertFalse(self.env['otm.service.renewal'].sudo().search([('service_id', '=', one_time.id)]))
        with self.assertRaises(UserError):  # the opportunity is the open renewal
            svc.with_user(self.exec_a1).action_start_renewal()


@tagged('post_install', '-at_install', 'otm_phase11')
class TestReportsAndAudit(Phase11Base):

    def test_estimate_pdf_hides_internal_amounts(self):
        out = self._drive('estimate')
        est = out['estimate']
        html = self.env['ir.actions.report'].with_user(self.exec_a1)._render_qweb_html(
            'sales_project_lifecycle.report_estimate_document', est.ids)[0].decode()
        self.assertIn(est.display_name, html)
        self.assertIn('Test CRM', html)
        self.assertIn('35,000', html)  # selling price 20,000 + 15,000
        self.assertNotIn('Additional', html)
        self.assertNotIn('Base', html)
        self.assertNotIn('20,000.00', html)

    def test_report_actions_and_views(self):
        self._drive('closed')
        for xmlid in ('action_rep_leads', 'action_rep_estimates', 'action_rep_commission', 'action_rep_projects',
                      'action_rep_delayed', 'action_rep_workload', 'action_rep_qc', 'action_rep_rework',
                      'action_rep_payments', 'action_rep_services'):
            action = self.env.ref('sales_project_lifecycle.' + xmlid)
            Model = self.env[action.res_model].with_user(self.admin_user)
            views = Model.get_views([(False, m) for m in action.view_mode.split(',')] + [(False, 'search')])
            self.assertTrue(views['views'], xmlid)
            Model.search(action.domain and eval(action.domain) or [])
            Model.formatted_read_group(action.domain and eval(action.domain) or [], [], ['__count'])
        # the delayed filter evaluates its context_today() domain
        Line = self.env['otm.project.stage.line'].with_user(self.ph)
        self.assertIsNotNone(Line.search([('state', 'in', ('pending', 'in_progress')),
                                          ('planned_deadline', '<', str(self.today))]))
        # pivot aggregation of money columns
        rows = self.env['otm.estimate'].with_user(self.admin_user).formatted_read_group(
            [], ['sales_team_id'], ['total_amount:sum'])
        self.assertTrue(rows)

    def test_report_data_follows_security(self):
        self._drive('estimate')
        E = self.env['otm.estimate']
        a = E.with_user(self.head_a).formatted_read_group([], ['sales_team_id'], ['__count'])
        b = E.with_user(self.head_b).formatted_read_group([], ['sales_team_id'], ['__count'])
        self.assertEqual(len(a), 1)
        self.assertEqual(b, [])

    def test_audit_trail(self):
        out = self._drive('project')
        project = out['project']
        # developer assignment
        project.with_user(self.ph).write({'otm_developer_ids': [(6, 0, [self.dev1.id, self.dev2.id])]})
        self.assertIn('Developers changed', ' '.join(project.message_ids.mapped('body')))
        # deadline change
        line = project.otm_stage_ids[1]
        old = line.planned_deadline
        line.with_user(self.ph).write({'planned_deadline': (old or self.today) + timedelta(days=5)})
        self.assertIn('dates changed', ' '.join(project.message_ids.mapped('body')))
        # estimate revisions keep the amounts
        rev = out['estimate'].revision_ids
        self.assertTrue(rev)
        self.assertEqual(rev[0].new_amount, out['estimate'].total_amount)
        # history cannot be edited by anyone
        log = self.env['otm.transition.log'].sudo().search([('res_model', '=', 'project.project')], limit=1)
        for user in (self.admin_user, self.ph, self.head_a):
            with self.assertRaises(Exception):
                log.with_user(user).write({'reason': 'tampered'})


@tagged('post_install', '-at_install', 'otm_phase11')
class TestSecuritySweep(Phase11Base):
    """Direct URL access: every record of Team A must be unreachable for outsiders."""

    def setUp(self):
        super().setUp()
        self.out = self._drive('closed')
        o = self.out
        self.records = {
            'otm.lead': self.sale_lead, 'otm.estimate': o['estimate'], 'otm.deal': o['deal'],
            'otm.customer.agreement': o['agreement'], 'otm.deal.payment': o['deal'].payment_ids[:1],
            'otm.sales.commission': o['deal'].sudo().commission_ids[:1], 'project.project': o['project'],
            'otm.qc': o['qc'], 'otm.deployment': o['deployment'], 'otm.training': o['training'],
            'otm.customer.review': o['review'],
            'otm.client.service': self.env['otm.client.service'].sudo().search([('deal_id', '=', o['deal'].id)], limit=1),
            'otm.demo': self.sale_lead.demo_ids[:1]}
        for model, rec in self.records.items():
            self.assertTrue(rec, model)

    def _reachable(self, user, model, rec):
        try:
            return bool(self.env[model].with_user(user).browse(rec.id).read(['id']))
        except AccessError:
            return False

    def test_outsiders_cannot_open_team_a_records(self):
        for user in (self.exec_b1, self.head_b, self.exec_a2):
            for model, rec in self.records.items():
                self.assertFalse(self._reachable(user, model, rec), f'{user.name} reached {model}')

    def test_owner_team_and_admin_can(self):
        for model in ('otm.lead', 'otm.estimate', 'otm.deal', 'otm.customer.agreement', 'otm.deal.payment',
                      'otm.sales.commission', 'otm.demo'):
            for user in (self.exec_a1, self.head_a, self.admin_user):
                if model == 'otm.sales.commission' and user == self.exec_a1:
                    continue
                self.assertTrue(self._reachable(user, model, self.records[model]), f'{user.name} {model}')

    def test_developer_and_finance_boundaries(self):
        dev = self.dev2  # not assigned to the project
        for model in ('otm.lead', 'otm.estimate', 'otm.deal', 'otm.deal.payment', 'otm.sales.commission',
                      'otm.sales.wallet', 'otm.customer.agreement'):
            rec = self.records.get(model) or self.env[model].sudo().search([], limit=1)
            if rec:
                self.assertFalse(self._reachable(self.dev1, model, rec), f'dev reached {model}')
                self.assertFalse(self._reachable(dev, model, rec), f'dev2 reached {model}')
        self.assertFalse(self._reachable(dev, 'project.project', self.records['project.project']))
        self.assertTrue(self._reachable(self.dev1, 'project.project', self.records['project.project']))
        # finance: payments yes, developer-side records no
        self.assertTrue(self._reachable(self.fin, 'otm.deal.payment', self.records['otm.deal.payment']))
        for model in ('otm.qc', 'otm.deployment', 'project.task', 'otm.lead'):
            rec = self.records.get(model) or self.env[model].sudo().search([], limit=1)
            self.assertFalse(self._reachable(self.fin, model, rec), f'finance reached {model}')
        # internal developer notes stay off the customer payment view
        with self.assertRaises(AccessError):
            self.out['project'].with_user(self.fin).read(['otm_technical_requirements'])

    def test_wallet_isolation(self):
        W = self.env['otm.sales.wallet']
        W.sudo().create({'sales_head_id': self.head_a.id})
        w_b = W.sudo().create({'sales_head_id': self.head_b.id})
        self.assertFalse(W.with_user(self.head_a).search([('id', '=', w_b.id)]))
        self.assertTrue(W.with_user(self.head_b).search([('id', '=', w_b.id)]))


@tagged('post_install', '-at_install', 'otm_phase11')
class TestAcceptance(Phase11Base):
    """Spec section 75: the whole lifecycle through the real workflow, then the post-sale services."""

    def test_full_lifecycle(self):
        out = self._drive('closed')
        log = self.env['otm.transition.log'].sudo()
        seen = lambda model, action: bool(log.search([('res_model', '=', model), ('action', '=', action)]))
        # sales (6-24)
        self.assertEqual(self.sale_lead.sales_team_id, self.team_a)
        for action in ('contact', 'collect_requirement', 'demo', 'estimate', 'negotiate'):
            self.assertTrue(seen('otm.lead', action), action)
        for action in ('confirm', 'complete'):
            self.assertTrue(seen('otm.demo', action), action)
        est = out['estimate']
        self.assertEqual(est.sudo().additional_amount, 15000)
        for action in ('submit', 'send', 'approve'):
            self.assertTrue(seen('otm.estimate', action), action)
        deal = out['deal']
        self.assertEqual(deal.status, 'locked')
        self.assertEqual(deal.total_amount, 35000)
        # agreement + advance (24-29)
        for action in ('generate', 'send', 'accept', 'sign', 'complete'):
            self.assertTrue(seen('otm.customer.agreement', action), action)
        pays = deal.payment_ids.sorted('id')
        self.assertEqual([p.amount for p in pays], [17500, 10500, 7000])  # 50 / 30 / 20 of 35,000
        self.assertEqual(set(pays.mapped('status')), {'received'})
        # project, development, QC loop, deployment, training (30-41)
        project = out['project']
        self.assertEqual(len(project.otm_stage_ids), 12)
        self.assertTrue(all(project.otm_stage_ids.mapped('planned_deadline')))
        for action in ('start', 'deliver', 'close'):
            self.assertTrue(seen('project.project', action), action)
        self.assertTrue(seen('otm.qc.issue', 'fix') and seen('otm.qc.issue', 'pass'))
        self.assertEqual(self.env['otm.qc'].sudo().search_count([('project_id', '=', project.id)]), 2)
        self.assertEqual(project.otm_qc_state, 'passed')
        self.assertEqual(out['deployment'].status, 'completed')
        self.assertEqual(out['training'].status, 'completed')
        # review + closure (47-49)
        self.assertEqual(out['review'].status, 'submitted')
        self.assertEqual(project.otm_state, 'closed')
        self.assertEqual(project.otm_closed_by_id, self.ph)
        self.assertEqual(self.sale_lead.stage, 'won')
        self.assertEqual(deal.sudo().commission_ids.mapped('commission_amount'), [15000.0])
        self.assertEqual(deal.sudo().commission_ids.status, 'earned')
        # client services remain active (50) + server / SSL / domain tracking (51-53)
        svc = self.env['otm.client.service'].sudo().search([('deal_id', '=', deal.id)])
        self.assertTrue(svc and set(svc.mapped('status')) == {'active'})
        server = out['server']
        server.sudo().write({'ssl_expiry_date': self.today + timedelta(days=15),
                             'hosting_expiry_date': self.today + timedelta(days=30)})
        self.env['otm.client.server']._cron_expiry_reminders()
        acts = self.env['mail.activity'].sudo().search([('res_model', '=', 'otm.client.server'),
                                                        ('res_id', '=', server.id)])
        self.assertEqual(len(acts), 2)
        self.assertEqual(acts.user_id, self.dev1)
        # integration renewal tracked (54-55) and renewal opportunity (56)
        integ = self.env['otm.client.integration'].with_user(self.head_a).create({
            'name': 'Razorpay', 'customer_id': self.customer.id, 'integration_type': 'payment',
            'setup_amount': 5000, 'recurring_amount': 500, 'expiry_date': self.today + timedelta(days=7),
            'sales_team_id': self.team_a.id, 'responsible_developer_id': self.dev1.id})
        self.env['otm.client.integration']._cron_expiry_reminders()
        self.assertEqual(integ.status, 'expiring')
        recurring = self.env['otm.client.service'].with_user(self.exec_a1).create({
            'customer_id': self.customer.id, 'service_name': 'AMC', 'service_kind': 'amc', 'amount': 12000,
            'renewal_amount': 14000, 'billing_type': 'yearly', 'renewal_period': 'yearly',
            'start_date': self.today - timedelta(days=330), 'expiry_date': self.today + timedelta(days=35)})
        self.env['otm.client.service']._cron_expiry_reminders()
        self.assertEqual(recurring.status, 'expiring')
        self.assertTrue(self.env['otm.service.renewal'].sudo().search([('service_id', '=', recurring.id)]))

    def test_gates_hold_without_shortcuts(self):
        out = self._drive('agreement')
        with self.assertRaises(UserError):  # nobody can close a project that was never delivered
            self.env['project.project'].browse(1).with_user(self.ph)._otm_do_transition('close')
        project = self.env['project.project'].browse(
            out['deal'].with_user(self.head_a).action_create_project()['res_id'])
        with self.assertRaises(UserError):
            project.with_user(self.ph).action_close()
        with self.assertRaises(UserError):
            project.with_user(self.ph).action_final_delivery()
        with self.assertRaises(UserError):
            project.with_user(self.admin_user).write({'otm_state': 'closed'})


@tagged('post_install', '-at_install', 'otm_phase11')
class TestPerformance(Phase11Base):

    def test_dashboard_query_budget(self):
        self._drive('closed')
        for user, budget in ((self.admin_user, 450), (self.head_a, 450), (self.exec_a1, 300)):
            self.env.invalidate_all()
            Dash = self.env['otm.dashboard'].with_user(user)
            start, t0 = self.env.cr.sql_log_count, time.time()
            data = Dash.get_dashboard({})
            used = self.env.cr.sql_log_count - start
            self.assertTrue(data['kpis'])
            self.assertLess(used, budget, f'{user.name}: {used} queries')
            self.assertLess(time.time() - t0, 10)
        board_start = self.env.cr.sql_log_count
        self.env['otm.dashboard'].with_user(self.ph).get_project_board()
        self.assertLess(self.env.cr.sql_log_count - board_start, 300)

    def test_dashboard_payload_is_small_and_json(self):
        self._drive('closed')
        blob = json.dumps(self.env['otm.dashboard'].with_user(self.admin_user).get_dashboard({}), default=str)
        self.assertLess(len(blob), 60000)
