from odoo import fields
from odoo.exceptions import AccessError, UserError, ValidationError
from odoo.tests import tagged

from .common import LifecycleCommon


@tagged('post_install', '-at_install', 'otm_agreement')
class TestAgreementFinance(LifecycleCommon):

    def setUp(self):
        super().setUp()
        G = self.env.ref
        self.fin = self.finance  # plain Finance user from the fixture
        self.fin_mgr = self.env['res.users'].with_context(no_reset_password=True).create({
            'name': 'Fin Mgr', 'login': 'tst_finmgr',
            'group_ids': [(6, 0, [G('sales_project_lifecycle.group_finance_manager').id])]})
        self.customer = self.env['res.partner'].create({'name': 'Cust'})
        lead = self.lead = self.lead_a1
        self._lead_to_estimate(lead)
        lead.with_user(self.exec_a1).action_negotiate()
        est = self.env['otm.estimate'].with_user(self.exec_a1).create({
            'lead_id': lead.id, 'customer_id': self.customer.id,
            'line_ids': [(0, 0, {'service_id': self.service.id})]})
        est.line_ids.with_user(self.head_a).write({'additional_amount': 15000})  # 35,000
        est.with_user(self.exec_a1).action_submit()
        est.with_user(self.head_a).action_send()
        est.with_user(self.head_a).action_approve()
        act = est.with_user(self.head_a).action_lock_deal(reason='ok')
        self.deal = self.env['otm.deal'].browse(act['res_id'])

    def _agreement(self):
        act = self.deal.with_user(self.exec_a1).action_create_agreement()
        return self.env['otm.customer.agreement'].browse(act['res_id'])

    def _signed(self, ag=None):
        ag = ag or self._agreement()
        u = self.exec_a1
        ag.with_user(u).action_generate()
        ag.with_user(u).action_send()
        ag.with_user(u).write({'customer_acceptance': 'Accepted by e-mail'})
        ag.with_user(u).action_accept()
        ag.with_user(u).write({'signed_document': 'UERG', 'signed_filename': 'signed.pdf',
                               'signed_date': fields.Date.today()})
        ag.with_user(u).action_sign()
        return ag

    def _confirm(self, pay, user=None, **kw):
        vals = {'payment_reference': 'UTR1', 'payment_method': 'bank', 'proof': 'UFJPT0Y=',
                'paid_date': fields.Date.today()}
        vals.update(kw)
        pay.with_user(user or self.fin).write(vals)
        pay.with_user(user or self.fin).action_confirm()

    def _advance(self, ag):
        return ag.payment_ids.filtered(lambda p: p.trigger == 'advance')

    # -- creation / security -------------------------------------------
    def test_create_defaults_and_guards(self):
        ag = self._agreement()
        self.assertEqual(ag.status, 'draft')
        self.assertTrue(ag.payment_schedule_id.is_default)
        self.assertIn('Test CRM', ag.deliverables)
        for user in (self.exec_a2, self.head_b, self.finance):
            with self.assertRaises(AccessError):
                self.deal.with_user(user).action_create_agreement()
        with self.assertRaises(UserError):
            self.deal.with_user(self.exec_a1).action_create_agreement()  # one active only

    def test_generate_builds_payment_lines(self):
        ag = self._agreement()
        ag.with_user(self.exec_a1).action_generate()
        pays = ag.payment_ids.sorted('sequence')
        self.assertEqual(pays.mapped('amount'), [17500, 10500, 7000])
        self.assertEqual(sum(pays.mapped('amount')), 35000)
        self.assertEqual(pays.mapped('status'), ['pending'] * 3)
        self.assertEqual(self.lead.stage, 'agreement')
        self.assertEqual(ag.final_amount, 35000)
        self.assertIn('Test CRM', ag.approved_services)

    def test_rounding_goes_to_last_installment(self):
        sch = self.env['otm.payment.schedule'].create({
            'name': 'Thirds', 'line_ids': [
                (0, 0, {'name': 'A', 'percentage': 33.33, 'trigger': 'advance'}),
                (0, 0, {'name': 'B', 'percentage': 33.33, 'trigger': 'after_training'}),
                (0, 0, {'name': 'C', 'percentage': 33.34, 'trigger': 'final_delivery'})]})
        ag = self._agreement()
        ag.with_user(self.exec_a1).write({'payment_schedule_id': sch.id})
        ag.with_user(self.exec_a1).action_generate()
        self.assertEqual(sum(ag.payment_ids.mapped('amount')), 35000)

    def test_content_locked_after_generate(self):
        ag = self._agreement()
        ag.with_user(self.exec_a1).action_generate()
        with self.assertRaises(UserError):
            ag.with_user(self.exec_a1).write({'scope': 'changed'})
        for user in (self.exec_a1, self.head_a, self.admin_user):
            with self.assertRaises(UserError):
                ag.with_user(user).write({'status': 'completed'})
            with self.assertRaises(UserError):
                ag.with_user(user).write({'final_amount': 1})

    # -- workflow ---------------------------------------------------------
    def test_invalid_transitions(self):
        ag = self._agreement()
        for m in ('action_send', 'action_accept', 'action_sign', 'action_complete'):
            with self.assertRaises(UserError):
                getattr(ag.with_user(self.head_a), m)()
        ag.with_user(self.exec_a1).action_generate()
        with self.assertRaises(UserError):
            ag.with_user(self.exec_a1).action_generate()
        ag.with_user(self.exec_a1).action_send()
        with self.assertRaises(UserError) as cm:
            ag.with_user(self.exec_a1).action_accept()
        self.assertIn('how the customer accepted', str(cm.exception))

    def test_sign_needs_document_and_complete_needs_head(self):
        ag = self._agreement()
        u = self.exec_a1
        ag.with_user(u).action_generate()
        ag.with_user(u).action_send()
        ag.with_user(u).write({'customer_acceptance': 'ok'})
        ag.with_user(u).action_accept()
        with self.assertRaises(UserError) as cm:
            ag.with_user(u).action_sign()
        self.assertIn('signed agreement', str(cm.exception).lower())
        ag.with_user(u).write({'signed_document': 'UERG', 'signed_date': fields.Date.today()})
        ag.with_user(u).action_sign()
        att = self.env['ir.attachment'].sudo().search([
            ('res_model', '=', 'res.partner'), ('res_id', '=', self.customer.id)])
        self.assertTrue(att)
        with self.assertRaises(AccessError):
            ag.with_user(u).action_complete()
        for other in (self.exec_a2, self.head_b):
            with self.assertRaises(AccessError):
                ag.with_user(other).action_complete()
        ag.with_user(self.head_a).action_complete()
        self.assertEqual(ag.status, 'completed')
        self.assertEqual(self.lead.stage, 'advance_pending')
        adv = self._advance(ag)
        self.assertEqual((adv.status, adv.due_date), ('due', fields.Date.today()))

    # -- project start gate ---------------------------------------------
    def test_project_start_gate(self):
        ag = self._agreement()
        self.assertFalse(self.deal.project_start_allowed)
        with self.assertRaises(UserError) as cm:
            self.deal._otm_project_start_check()
        self.assertIn('Project cannot start until', str(cm.exception))
        self._signed(ag)
        ag.with_user(self.head_a).action_complete()
        self.assertFalse(self.deal.project_start_allowed)  # agreement done, advance missing
        with self.assertRaises(UserError):
            self.lead._otm_do_transition('system_project')
        self._confirm(self._advance(ag))
        self.assertTrue(self.deal.project_start_allowed)
        self.assertTrue(self.deal._otm_project_start_check())
        self.lead._otm_do_transition('system_project')
        self.assertEqual(self.lead.stage, 'project')

    def test_advance_alone_is_not_enough(self):
        ag = self._signed()
        # agreement only signed (not completed): advance is still not due
        self.assertEqual(self._advance(ag).status, 'pending')
        with self.assertRaises(UserError):
            self._confirm(self._advance(ag))
        self.assertFalse(self.deal.project_start_allowed)

    # -- finance ------------------------------------------------------------
    def _completed(self):
        ag = self._signed()
        ag.with_user(self.head_a).action_complete()
        return ag

    def test_finance_confirmation_rules(self):
        ag = self._completed()
        adv = self._advance(ag)
        for user in (self.exec_a1, self.head_a, self.developer):
            with self.assertRaises(AccessError):
                adv.with_user(user).action_confirm()
        with self.assertRaises(UserError) as cm:  # details missing
            adv.with_user(self.fin).action_confirm()
        self.assertIn('payment reference', str(cm.exception))
        with self.assertRaises(UserError):
            adv.with_user(self.fin).write({'paid_date': fields.Date.today().replace(year=2999)})
            adv.with_user(self.fin).write({'payment_reference': 'X', 'payment_method': 'upi'})
            adv.with_user(self.fin).action_confirm()
        with self.assertRaises(Exception):
            adv.with_user(self.exec_a1).write({'payment_reference': 'FAKE'})
        for user in (self.fin, self.admin_user):
            with self.assertRaises(UserError):
                adv.with_user(user).write({'amount': 1})
            with self.assertRaises(UserError):
                adv.with_user(user).write({'status': 'received'})

    def test_payment_received_updates_totals_and_commission(self):
        ag = self._completed()
        adv = self._advance(ag)
        self._confirm(adv)
        self.assertEqual(adv.status, 'received')
        self.assertEqual(adv.confirmed_by_id, self.fin)
        self.assertEqual((self.deal.amount_received, self.deal.balance_due), (17500, 17500))
        self.assertEqual(self.deal.commission_ids.status, 'earned')  # advance trigger
        # a received payment is frozen
        with self.assertRaises(UserError):
            adv.with_user(self.fin).write({'payment_reference': 'CHANGED'})
        with self.assertRaises(UserError):
            adv.with_user(self.fin).action_confirm()

    def test_later_installments_follow_events(self):
        ag = self._completed()
        self._confirm(self._advance(ag))
        training = ag.payment_ids.filtered(lambda p: p.trigger == 'after_training')
        final = ag.payment_ids.filtered(lambda p: p.trigger == 'final_delivery')
        self.assertEqual((training.status, final.status), ('pending', 'pending'))
        self.deal._otm_payment_event('after_training')
        self.assertEqual(training.status, 'due')
        self.assertEqual(final.status, 'pending')
        self._confirm(training, user=self.fin_mgr)
        self.deal._otm_payment_event('final_delivery')
        training.with_user(self.fin).env.cr  # noqa
        final.with_user(self.exec_a1).action_request()
        self._confirm(final)
        self.assertEqual(self.deal.balance_due, 0)
        self.assertEqual(self.deal.amount_received, 35000)

    def test_cancel_payment_roles(self):
        ag = self._completed()
        adv = self._advance(ag)
        with self.assertRaises(AccessError):
            adv.with_user(self.fin).action_cancel(reason='x')
        with self.assertRaises(UserError):
            adv.with_user(self.fin_mgr).action_cancel()
        adv.with_user(self.fin_mgr).action_cancel(reason='Wrong schedule')
        self.assertEqual(adv.status, 'cancelled')

    # -- cancellation ----------------------------------------------------
    def test_cancel_agreement(self):
        ag = self._agreement()
        ag.with_user(self.exec_a1).action_generate()
        with self.assertRaises(AccessError):
            ag.with_user(self.exec_a1).action_cancel(reason='x')
        with self.assertRaises(UserError):
            ag.with_user(self.head_a).action_cancel()
        ag.with_user(self.head_a).action_cancel(reason='Re-scope')
        self.assertEqual(ag.status, 'cancelled')
        self.assertEqual(set(ag.payment_ids.mapped('status')), {'cancelled'})
        self.assertEqual(self.lead.stage, 'deal_locked')
        self.assertTrue(self._agreement())  # a new one can be created

    def test_cancel_blocked_after_payment(self):
        ag = self._completed()
        self._confirm(self._advance(ag))
        with self.assertRaises(UserError):
            ag.with_user(self.head_a).action_cancel(reason='x')
        with self.assertRaises(UserError):
            self.deal.with_user(self.head_a).action_cancel(reason='x')

    def test_deal_revision_needs_agreement_cancelled(self):
        ag = self._agreement()
        with self.assertRaises(UserError) as cm:
            self.deal.with_user(self.head_a).action_revise(reason='x')
        self.assertIn('Cancel the customer agreement', str(cm.exception))
        ag.with_user(self.head_a).action_cancel(reason='x')
        self.deal.with_user(self.head_a).action_revise(reason='x')
        self.assertEqual(self.deal.status, 'revision')

    def test_deal_cancel_cascades(self):
        ag = self._agreement()
        ag.with_user(self.exec_a1).action_generate()
        self.deal.with_user(self.head_a).action_cancel(reason='Lost')
        self.assertEqual(ag.status, 'cancelled')
        self.assertEqual(set(ag.payment_ids.mapped('status')), {'cancelled'})
        self.assertEqual(self.lead.stage, 'negotiation')

    # -- schedule configuration -------------------------------------------
    def test_schedule_validation(self):
        S = self.env['otm.payment.schedule']
        with self.assertRaises(ValidationError):
            S.create({'name': 'Bad', 'line_ids': [
                (0, 0, {'name': 'A', 'percentage': 50, 'trigger': 'advance'}),
                (0, 0, {'name': 'B', 'percentage': 40, 'trigger': 'final_delivery'})]})
        with self.assertRaises(ValidationError):
            S.create({'name': 'NoAdvance', 'line_ids': [
                (0, 0, {'name': 'A', 'percentage': 100, 'trigger': 'final_delivery'})]})
        with self.assertRaises(ValidationError):
            S.create({'name': 'Second default', 'is_default': True, 'line_ids': [
                (0, 0, {'name': 'A', 'percentage': 100, 'trigger': 'advance'})]})

    def test_custom_schedule_not_hardcoded(self):
        sch = self.env['otm.payment.schedule'].create({'name': '40/60', 'line_ids': [
            (0, 0, {'name': 'Advance', 'percentage': 40, 'trigger': 'advance'}),
            (0, 0, {'name': 'Final', 'percentage': 60, 'trigger': 'final_delivery'})]})
        ag = self._agreement()
        ag.with_user(self.exec_a1).write({'payment_schedule_id': sch.id})
        ag.with_user(self.exec_a1).action_generate()
        self.assertEqual(sorted(ag.payment_ids.mapped('amount')), [14000, 21000])

    # -- visibility / report --------------------------------------------------
    def test_isolation(self):
        ag = self._completed()
        A, P = self.env['otm.customer.agreement'], self.env['otm.deal.payment']
        for model, rec in ((A, ag), (P, ag.payment_ids[:1])):
            for user in (self.exec_a1, self.head_a, self.finance, self.admin_user):
                self.assertTrue(model.with_user(user).search([('id', '=', rec.id)]), user.name)
            for user in (self.exec_a2, self.head_b, self.exec_b1):
                self.assertFalse(model.with_user(user).search([('id', '=', rec.id)]), user.name)

    def test_agreement_report_renders(self):
        ag = self._completed()
        html, _fmt = self.env['ir.actions.report']._render_qweb_html(
            'sales_project_lifecycle.action_report_agreement', ag.ids)
        text = html.decode()
        self.assertIn(ag.agreement_number, text)
        self.assertIn('Scope of Work', text)
        self.assertIn('Advance before development', text)
        with self.assertRaises(UserError):
            self._agreement_draft_print()

    def _agreement_draft_print(self):
        self.deal.sudo().agreement_ids.sudo()._otm_do_transition('system_cancel')
        act = self.deal.with_user(self.exec_a1).sudo().action_create_agreement()
        self.env['otm.customer.agreement'].browse(act['res_id']).action_print()

    def test_history_recorded(self):
        ag = self._completed()
        logs = self.env['otm.transition.log'].search([
            ('res_model', '=', 'otm.customer.agreement'), ('res_id', '=', ag.id)], order='id')
        self.assertEqual([l.to_state for l in logs],
                         ['Generated', 'Sent', 'Customer Accepted', 'Signed', 'Completed'])
