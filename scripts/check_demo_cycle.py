"""Isolated SQL audit of the main demo cycle; exchange responses are deterministic fixtures.

Run against a disposable local MariaDB/MySQL using AUDIT_DB_PORT (default 43317).
Creates a uniquely named audit database, never connects to the configured application DB.
"""
import os,sys,time,json,logging,uuid
from pathlib import Path
from decimal import Decimal as D
from datetime import datetime,timezone,timedelta
from unittest.mock import patch
from contextlib import ExitStack
import mysql.connector
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from main.treider import trade,account
from main.treider.market_data import SpotBookTicker,SpotOrderBook,simulate_market_sell
from main.treider.market_rules import SpotMarketRules
from main.treider.fees import TradeFeeRates
from database import initialize_database
logging.disable(logging.CRITICAL)
DB='arb_audit_'+uuid.uuid4().hex[:12]
def conn(db=True):
 return mysql.connector.connect(host='127.0.0.1',port=int(os.getenv('AUDIT_DB_PORT','43317')),user=os.getenv('AUDIT_DB_USER','root'),password=os.getenv('AUDIT_DB_PASSWORD',''),database=DB if db else None)
for attempt in range(50):
 try:
  c=conn(False);break
 except Exception:time.sleep(.1)
else:raise RuntimeError('DB start failed')
q=c.cursor();q.execute('CREATE DATABASE '+DB);c.close()
c=conn()
schema=(Path(__file__).resolve().parents[1]/'schema.sql').read_text()
# MariaDB needs CURRENT_TIMESTAMP in DEFAULT / ON UPDATE clauses. Runtime SQL stays unchanged.
if 'MariaDB' in c.server_info:
 schema=schema.replace('DEFAULT UTC_TIMESTAMP(6)','DEFAULT CURRENT_TIMESTAMP(6)').replace('ON UPDATE UTC_TIMESTAMP(6)','ON UPDATE CURRENT_TIMESTAMP(6)')
with patch('database.Path.read_text',return_value=schema): initialize_database(c)
account.initialize_trader_account(c);run=account.active_demo_run_id(c);trade.set_demo_run_id(run);c.close()
def query(sql,args=()):
 c=conn();q=c.cursor(dictionary=True);q.execute(sql,args);r=q.fetchall() if q.with_rows else [];c.commit();c.close();return r
now=lambda:datetime.now(timezone.utc)
rules=SpotMarketRules(D('.001'),D('.001'),D('1'),'audit')
fees=lambda ex:TradeFeeRates(ex,'ABCUSDT',D('0'),D('.001'),'audit')
book=lambda ex,p:D(p)
def depth(ex,pair):
 p='10' if ex=='binance' else '11'
 return SpotOrderBook(ex,pair,[[D(p),D('100')]],[[D(p),D('100')]],now())
transfer={'withdraw_fee':'0.1','withdraw_fee_currency':'ABC','route_status':'unconfirmed','network':'DEMO','estimated_seconds':300,'min_withdraw':'0.001','source_network':{'withdraw_step':'.001','min_withdraw':'.001'}}
results=[]
with ExitStack() as stack:
 stack.enter_context(patch.object(trade,'connect_db',conn))
 stack.enter_context(patch.object(trade,'fetch_spot_market_rules',return_value=rules))
 stack.enter_context(patch.object(trade,'fetch_spot_order_book',side_effect=depth))
 stack.enter_context(patch.object(trade,'get_trade_fee_rates',side_effect=lambda ex,p:fees(ex)))
 stack.enter_context(patch.object(trade,'TRADE_SIZE_CANDIDATES_USDT',(D('100'),)))
 stack.enter_context(patch.object(trade,'SAME_ROUTE_REENTRY_COOLDOWN_SECONDS',0))
 stack.enter_context(patch.object(trade,'DEMO_STRATEGY','immediate_after_transfer'))
 buy=SpotBookTicker('binance','ABC/USDT',D('10'),D('100'),D('10'),D('100'))
 sell=SpotBookTicker('mexc','ABC/USDT',D('11'),D('100'),D('11'),D('100'))
 for i,(name,price,partial,fail) in enumerate([('profit','11',False,False),('loss','9',False,False),('partial','11',True,False),('api_retry','11',False,True),('stop_loss','9',False,False)],1):
  query('UPDATE trader_balances SET balance=0');query("INSERT INTO trader_balances(exchange_slug,asset,balance) VALUES('binance','USDT',100) ON DUPLICATE KEY UPDATE balance=100")
  query("INSERT INTO trader_signals(opportunity_key,payload) VALUES(%s,%s)",(f'audit-{i}',json.dumps({'audit':True})))
  sid=query('SELECT MAX(signal_id) AS id FROM trader_signals')[0]['id']
  trade.DEMO_STRATEGY='tp_sl' if name=='stop_loss' else 'immediate_after_transfer'
  signal={'source_pair':'ABC/USDT','sell':{'price':'11'},'opportunity_started_at_utc':now().isoformat()}
  estimate,reason=trade._prepare_training_trade(signal,sid,buy,sell,fees('binance'),fees('mexc'),300,transfer)
  row=query('SELECT * FROM trader_training_settlements WHERE signal_id=%s',(sid,))[0]
  assert trade.process_due_training_trade() is False,'sale before arrival'
  query('UPDATE trader_training_settlements SET settle_after_utc=UTC_TIMESTAMP() - INTERVAL 1 SECOND WHERE settlement_id=%s',(row['settlement_id'],))
  calls=[0]
  def execution(ex,pair,qty,**kw):
   calls[0]+=1
   if fail and calls[0]==1:raise TimeoutError('audit offline')
   size=D('2') if partial and calls[0]==1 else D('100')
   return simulate_market_sell(ex,pair,qty,[[price,str(size)]],**kw)
  with patch.object(trade,'fetch_spot_book',return_value=SpotBookTicker('mexc','ABC/USDT',D(price),D('100'),D(price),D('100'))),patch.object(trade,'fetch_market_sell_execution',side_effect=execution):
   trade.process_due_training_trade()
   intermediate=query('SELECT * FROM trader_training_settlements WHERE signal_id=%s',(sid,))[0]
   if partial or fail:
    query('UPDATE trader_training_settlements SET settle_after_utc=UTC_TIMESTAMP() - INTERVAL 1 SECOND WHERE signal_id=%s',(sid,))
    trade.process_due_training_trade()
   assert trade.process_due_training_trade() is False,'duplicate settlement'
  final=query('SELECT * FROM trader_training_settlements WHERE signal_id=%s',(sid,))[0]
  cash=query("SELECT balance FROM trader_balances WHERE exchange_slug='mexc' AND asset='USDT'")[0]['balance']
  expected=estimate.quantity_after_fees*D(price)*D('.999')
  assert abs(cash-expected)<D('.00000001'),(cash,expected)
  assert final['status']=='settled',final['last_error']
  assert not query('SELECT * FROM trader_balances WHERE balance < 0'),'negative balance'
  deltas=query("SELECT exchange_slug,asset,SUM(quantity) AS delta FROM trader_demo_operation_log WHERE settlement_id=%s AND event_type='balance_change' GROUP BY exchange_slug,asset",(final['settlement_id'],))
  for delta in deltas:
   actual=query('SELECT balance FROM trader_balances WHERE exchange_slug=%s AND asset=%s',(delta['exchange_slug'],delta['asset']))
   baseline=D('100') if (delta['exchange_slug'],delta['asset'])==('binance','USDT') else D(0)
   assert abs(actual[0]['balance']-baseline-delta['delta'])<D('.00000001'),'ledger/balance mismatch' 
  assert abs(final['realized_profit_usd']-(expected-final['buy_debit_usd']))<D('.00000001')
  results.append({'case':name,'buy_debit':str(final['buy_debit_usd']),'received':str(estimate.quantity_after_fees),'cash':str(cash),'pnl':str(final['realized_profit_usd']),'intermediate_status':intermediate['status']})
before=query("SELECT SUM(balance) AS amount FROM trader_balances WHERE asset='USDT'")[0]['amount']
c=conn();account.initialize_trader_account(c);c.close()
after=query("SELECT SUM(balance) AS amount FROM trader_balances WHERE asset='USDT'")[0]['amount']
assert before==after,'restart changed balance'
print(json.dumps(results,indent=2))
print('REINITIALIZATION: capital unchanged')
