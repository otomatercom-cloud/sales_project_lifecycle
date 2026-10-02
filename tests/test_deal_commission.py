from odoo.exceptions import AccessError, UserError
from odoo.tests import tagged

from .common import LifecycleCommon


@tagged('post_install', '-at_install', 'otm_deal')
class TestDealCommission(LifecycleCommon):

    def setUp(self):
        super().setUp()
        self.fin_mgr = self.env['res.users'].with_context(no_reset_password=True).create({
            'name': 'Fin Mgr', 'login': 'tst_finmgr',
            'group_ids': [(6, 0, [self.env.ref('sales_project_lifecycle.group_finance_manager').id])]})
        self.customer = self.env['res.partner'].create({'name': 'Cust'})
        self.hrms = self.env['otm.service'].create({'name': 'HRMS', 'code': 'TST-HR', 'base_amount': 30000})
        self.web = self.env['otm.service'].create({'name': 'Web', 'code': 'TST-WEB', 'base_amount': 25000})
        self.rule = self.env.ref('sales_project_lifecycle.commission_rule_default')
        self.lead = self.lead_a1
        self._lead_to_estimate(self.lead)
        self.lead.with_user(self.exec_a1).action_negotiate()

    def _estimate(self, services=None, approve=True):
        services = services or [(self.service, 15000)]
        est = self.env['otm.estimate'].with_user(self.exec_a1).create({
            'lead_id': self.lead.id, 'customer_id': self.customer.id,
            'line_ids': [(0, 0, {'service_id': s.id}) for s, _a in services]})
        for line, (_s, add) in zip(est.line_ids, services):
            line.with_user(self.head_a).write({'additional_amount': add})
        if approve:
            est.with_user(self.exec_a1).action_submit()
            est.with_user(self.head_a).action_send()
            est.with_user(self.head_a).action_approve()
        return est

    def _lock(self, est=None, **kw):
        est = est or self._estimate(**kw)
        deal = est.with_user(self.head_a).action_lock_deal(reason='Terms agreed')
        return est, self.env['otm.deal'].browse(deal['res_id'])

    def _earned_commission(self):
        est, deal = self._lock()
        deal._otm_commission_event('advance_received')
        return deal, deal.commission_ids

    # -- lock ---------------------------------------------------------
    def test_lock_snapshot_and_commission(self):
        est, deal = self._lock()
        self.assertEqual(deal.status, 'locked')
        self.assertEqual(self.lead.stage, 'deal_locked')
        self.assertEqual((deal.base_total, deal.additional_total, deal.selling_total),
                         (20000, 15000, 35000))
        self.assertEqual(deal.locked_by_id, self.head_a)
        self.assertEqual(deal.lock_reason, 'Terms agreed')
        c = deal.commission_ids
        self.assertEqual(len(c), 1)
        self.assertEqual((c.status, c.commission_amount), ('pending', 15000))
        self.assertAlmostEqual(c.additional_percentage, 75.0)
        self.assertAlmostEqual(c.commission_percentage, 75.0)
        self.assertEqual(c.sales_head_id, self.head_a)

    def test_multi_service_totals(self):
        _e, deal = self._lock(services=[(self.service, 15000), (self.hrms, 10000), (self.web, 5000)])
        self.assertEqual((deal.base_total, deal.additional_total, deal.selling_total),
                         (75000, 30000, 105000))
        self.assertEqual(len(deal.commission_ids), 3)
        self.assertEqual(deal.commission_total, 30000)

    def test_lock_guards(self):
        est = self._estimate()
        with self.assertRaises(UserError):
            est.with_user(self.head_a).action_lock_deal(reason=' ')
        for user in (self.exec_a1, self.exec_a2, self.head_b, self.finance):
            with self.assertRaises(AccessError):
                est.with_user(user).action_lock_deal(reason='x')
        # estimate must be approved
        draft = self._estimate(approve=False)
        with self.assertRaises(UserError) as cm:
            draft.with_user(self.head_a).action_lock_deal(reason='x')
        self.assertIn('approved', str(cm.exception))
        est.with_user(self.head_a).action_lock_deal(reason='ok')
        with self.assertRaises(UserError):  # already has an active deal
            est.with_user(self.head_a).action_lock_deal(reason='again')

    def test_lead_must_be_in_negotiation(self):
        lead = self.lead_a2
        self._lead_to_estimate(lead)
        est = self.env['otm.estimate'].with_user(self.exec_a2).create({
            'lead_id': lead.id, 'customer_id': self.customer.id,
            'line_ids': [(0, 0, {'service_id': self.service.id})]})
        est.line_ids.with_user(self.head_a).write({'additional_amount': 100})
        est.with_user(self.exec_a2).action_submit()
        est.with_user(self.head_a).action_send()
        est.with_user(self.head_a).action_approve()
        with self.assertRaises(UserError) as cm:
            est.with_user(self.head_a).action_lock_deal(reason='x')
        self.assertIn('Negotiation', str(cm.exception))

    def test_deal_is_protected(self):
        _e, deal = self._lock()
        for user in (self.head_a, self.admin_user):
            with self.assertRaises(UserError):
                deal.with_user(user).write({'total_amount': 1})
            with self.assertRaises(UserError):
                deal.with_user(user).write({'status': 'cancelled'})
            with self.assertRaises(UserError):
                deal.line_ids.with_user(user).write({'additional_amount': 1})
            with self.assertRaises(Exception):
                deal.with_user(user).unlink()
        deal.with_user(self.head_a).write({'notes': 'ok'})

    def test_estimate_cannot_be_revised_while_locked(self):
        est, _deal = self._lock()
        with self.assertRaises(UserError) as cm:
            est.with_user(self.head_a).action_revise(reason='change')
        self.assertIn('locked', str(cm.exception))

    # -- controlled revision -----------------------------------------
    def test_deal_revision_and_relock(self):
        est, deal = self._lock()
        with self.assertRaises(UserError):
            deal.with_user(self.head_a).action_revise()
        for user in (self.exec_a1, self.head_b):
            with self.assertRaises(AccessError):
                deal.with_user(user).action_revise(reason='x')
        deal.with_user(self.head_a).action_revise(reason='Scope change')
        self.assertEqual(deal.status, 'revision')
        with self.assertRaises(UserError):  # nothing new yet
            deal.with_user(self.head_a).action_relock(reason='x')
        est.with_user(self.head_a).action_revise(reason='Scope change')
        est.line_ids.with_user(self.head_a).write({'additional_amount': 20000})
        est.with_user(self.exec_a1).action_submit()
        est.with_user(self.head_a).action_send()
        est.with_user(self.head_a).action_approve()
        deal.with_user(self.head_a).action_relock(reason='New terms')
        self.assertEqual(deal.status, 'locked')
        self.assertEqual(deal.selling_total, 40000)
        self.assertEqual(deal.estimate_revision, 2)
        live = deal.commission_ids.filtered(lambda c: c.status == 'pending')
        self.assertEqual(len(live), 1)
        self.assertEqual(live.commission_amount, 20000)
        self.assertEqual(len(deal.commission_ids.filtered(lambda c: c.status == 'cancelled')), 1)

    def test_cancel_deal(self):
        _e, deal = self._lock()
        deal.with_user(self.head_a).action_cancel(reason='Customer left')
        self.assertEqual(deal.status, 'cancelled')
        self.assertEqual(self.lead.stage, 'negotiation')
        self.assertEqual(deal.commission_ids.status, 'cancelled')
        with self.assertRaises(UserError):
            deal.with_user(self.head_a).action_cancel(reason='again')

    def test_cancel_blocked_with_approved_commission(self):
        deal, comm = self._earned_commission()
        comm.with_user(self.fin_mgr).action_approve()
        with self.assertRaises(UserError):
            deal.with_user(self.head_a).action_cancel(reason='x')
        with self.assertRaises(UserError):
            deal.with_user(self.head_a).action_revise(reason='x')

    # -- commission lifecycle & wallet ---------------------------------
    def test_commission_flow_and_wallet(self):
        deal, comm = self._earned_commission()
        self.assertEqual(comm.status, 'earned')  # approval required
        wallet_model = self.env['otm.sales.wallet']
        self.assertFalse(wallet_model.sudo().search([('sales_head_id', '=', self.head_a.id)]).balance)
        with self.assertRaises(UserError):  # earned cannot be paid
            comm.with_user(self.fin_mgr).action_pay()
        comm.with_user(self.fin_mgr).action_approve()
        wallet = wallet_model.sudo().search([('sales_head_id', '=', self.head_a.id)])
        self.assertEqual(wallet.balance, 15000)
        comm.with_user(self.fin_mgr).action_pay()
        self.assertEqual(comm.status, 'paid')
        self.assertEqual(wallet.balance, 0)
        types = wallet.transaction_ids.mapped('transaction_type')
        self.assertCountEqual(types, ['commission_credit', 'commission_payout'])
        self.assertEqual(wallet.total_credited, 15000)
        self.assertEqual(wallet.total_paid, 15000)

    def test_event_matching_trigger_only(self):
        _e, deal = self._lock()
        deal._otm_commission_event('final_payment')
        self.assertEqual(deal.commission_ids.status, 'pending')
        deal._otm_commission_event('advance_received')
        self.assertEqual(deal.commission_ids.status, 'earned')

    def test_no_approval_required_credits_wallet(self):
        self.rule.sudo().approval_required = False
        deal, comm = self._earned_commission()
        self.assertEqual(comm.status, 'approved')
        wallet = self.env['otm.sales.wallet'].sudo().search([('sales_head_id', '=', self.head_a.id)])
        self.assertEqual(wallet.balance, 15000)

    def test_rule_percentage_and_cap(self):
        rule = self.env['otm.commission.rule'].create({
            'name': 'Half capped', 'percentage': 50, 'max_amount': 5000,
            'trigger': 'final_payment'})
        self.service.sudo().commission_rule_id = rule
        _e, deal = self._lock()
        c = deal.commission_ids
        self.assertEqual(c.commission_amount, 5000)  # 7500 capped at 5000
        self.assertEqual(c.trigger, 'final_payment')
        self.assertAlmostEqual(c.commission_percentage, 25.0)

    def test_commission_disabled_service(self):
        self.service.sudo().commission_enabled = False
        _e, deal = self._lock()
        self.assertFalse(deal.commission_ids)

    def test_reverse_and_cancel(self):
        deal, comm = self._earned_commission()
        comm.with_user(self.fin_mgr).action_cancel(reason='Customer refund')
        self.assertEqual(comm.status, 'cancelled')
        wallet = self.env['otm.sales.wallet'].sudo().search([('sales_head_id', '=', self.head_a.id)])
        self.assertFalse(wallet.balance)

    def test_reverse_approved_and_paid(self):
        deal, comm = self._earned_commission()
        comm.with_user(self.fin_mgr).action_approve()
        with self.assertRaises(UserError):
            comm.with_user(self.fin_mgr).action_reverse()  # reason mandatory
        comm.with_user(self.fin_mgr).action_reverse(reason='Payment bounced')
        wallet = self.env['otm.sales.wallet'].sudo().search([('sales_head_id', '=', self.head_a.id)])
        self.assertEqual((comm.status, wallet.balance), ('reversed', 0))
        with self.assertRaises(UserError):
            comm.with_user(self.fin_mgr).action_pay()

    def test_pay_needs_balance(self):
        deal, comm = self._earned_commission()
        comm.with_user(self.fin_mgr).action_approve()
        wallet = self.env['otm.sales.wallet'].sudo().search([('sales_head_id', '=', self.head_a.id)])
        wallet.with_user(self.fin_mgr).action_add_adjustment(amount=-10000, notes='Advance recovered')
        with self.assertRaises(UserError) as cm:
            comm.with_user(self.fin_mgr).action_pay()
        self.assertIn('wallet balance', str(cm.exception))
        wallet.with_user(self.fin_mgr).action_add_adjustment(amount=10000, notes='Corrected')
        comm.with_user(self.fin_mgr).action_pay()
        self.assertEqual(wallet.balance, 0)

    def test_commission_roles(self):
        deal, comm = self._earned_commission()
        for user in (self.head_a, self.exec_a1, self.finance, self.head_b):
            with self.assertRaises(AccessError):
                comm.with_user(user).action_approve()
        with self.assertRaises(UserError):
            comm.with_user(self.fin_mgr).write({'commission_amount': 1})
        with self.assertRaises(UserError):
            comm.with_user(self.admin_user).write({'status': 'paid'})
        with self.assertRaises(UserError):
            comm.with_user(self.admin_user).unlink()
        with self.assertRaises(UserError):
            self.env['otm.sales.commission'].with_user(self.admin_user).create({
                'deal_id': deal.id, 'sales_head_id': self.head_a.id, 'trigger': 'final_payment'})

    # -- wallet / ledger security ---------------------------------------
    def test_wallet_security(self):
        deal, comm = self._earned_commission()
        comm.with_user(self.fin_mgr).action_approve()
        Wallet = self.env['otm.sales.wallet']
        Txn = self.env['otm.sales.wallet.transaction']
        own = Wallet.with_user(self.head_a).search([])
        self.assertEqual(own.sales_head_id, self.head_a)
        self.assertEqual(own.balance, 15000)
        self.assertFalse(Wallet.with_user(self.head_b).search([]))
        self.assertFalse(Txn.with_user(self.head_b).search([]))
        self.assertTrue(Txn.with_user(self.head_a).search([]))
        self.assertTrue(Wallet.with_user(self.finance).search([]))
        self.assertTrue(Wallet.with_user(self.admin_user).search([]))
        with self.assertRaises(AccessError):
            Wallet.with_user(self.exec_a1).search([])
        with self.assertRaises(AccessError):
            Txn.with_user(self.exec_a1).search([])
        # balance can never be typed, ledger is append-only
        for user in (self.head_a, self.fin_mgr, self.admin_user):
            with self.assertRaises(Exception):
                own.with_user(user).write({'balance': 999999})
        txn = Txn.sudo().search([], limit=1)
        with self.assertRaises(UserError):
            txn.with_user(self.admin_user).write({'amount': 1})
        with self.assertRaises(UserError):
            txn.with_user(self.admin_user).unlink()
        with self.assertRaises(Exception):
            Txn.with_user(self.admin_user).create({
                'wallet_id': own.id, 'transaction_type': 'commission_credit', 'amount': 5})
        with self.assertRaises(AccessError):
            own.with_user(self.head_a).action_add_adjustment(amount=100, notes='self-pay')
        with self.assertRaises(UserError):
            own.with_user(self.fin_mgr).action_add_adjustment(amount=100)  # note mandatory

    def test_commission_isolation_and_field_security(self):
        deal, comm = self._earned_commission()
        C = self.env['otm.sales.commission']
        self.assertTrue(C.with_user(self.head_a).search([('id', '=', comm.id)]))
        self.assertFalse(C.with_user(self.head_b).search([('id', '=', comm.id)]))
        with self.assertRaises(AccessError):
            C.with_user(self.exec_a1).search([])
        D = self.env['otm.deal']
        self.assertTrue(D.with_user(self.exec_a1).search([('id', '=', deal.id)]))
        self.assertFalse(D.with_user(self.exec_a2).search([('id', '=', deal.id)]))
        self.assertFalse(D.with_user(self.head_b).search([('id', '=', deal.id)]))
        self.assertTrue(D.with_user(self.finance).search([('id', '=', deal.id)]))
        # the executive sees neither the deal value nor the margin: amounts are for Sales Head / Finance / Admin
        d = deal.with_user(self.exec_a1)
        with self.assertRaises(AccessError):
            d.read(['total_amount'])
        with self.assertRaises(AccessError):
            d.read(['balance_due'])
        self.assertEqual(deal.with_user(self.head_a).total_amount, 35000)
        self.assertEqual(deal.with_user(self.finance).total_amount, 35000)
        with self.assertRaises(AccessError):
            d.read(['additional_total'])
        with self.assertRaises(AccessError):
            d.line_ids.read(['additional_amount'])
        self.assertTrue(deal.with_user(self.finance).read(['additional_total']))

    def test_history_recorded(self):
        deal, comm = self._earned_commission()
        comm.with_user(self.fin_mgr).action_approve()
        logs = self.env['otm.transition.log'].search([
            ('res_model', '=', 'otm.sales.commission'), ('res_id', '=', comm.id)], order='id')
        self.assertEqual([l.to_state for l in logs], ['Earned', 'Approved'])
        dlogs = self.env['otm.transition.log'].search([
            ('res_model', '=', 'otm.deal'), ('res_id', '=', deal.id)])
        self.assertEqual(dlogs.mapped('to_state'), ['Locked'])
        self.assertEqual(dlogs.reason, 'Terms agreed')
