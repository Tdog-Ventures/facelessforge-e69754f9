import React, { useState, useEffect } from 'react';
export default function BillingBankTransfer() {
  const [loading, setLoading] = useState(false);
  const [data, setData] = useState<any>(null);
  const [status, setStatus] = useState<any>(null);
  const [error, setError] = useState('');
  const createTransfer = async () => {
    setLoading(true); setError('');
    try {
      const res = await fetch('/api/billing/create-bank-transfer', { method: 'POST', headers: { 'Content-Type': 'application/json' }, credentials: 'include', body: JSON.stringify({ plan: 'pro' }) });
      const json = await res.json(); if (!res.ok) throw new Error(json.detail || 'Failed'); setData(json); checkStatus(json.reference);
    } catch (e: any) { setError(e.message); } finally { setLoading(false); }
  };
  const checkStatus = async (ref?: string) => {
    const reference = ref || data?.reference; if (!reference) return;
    try { const res = await fetch(`/api/billing/status?reference=${reference}`, { credentials: 'include' }); const json = await res.json(); setStatus(json); } catch {}
  };
  const ivePaid = async () => {
    if (!data?.reference) return; setLoading(true);
    try {
      const res = await fetch(`/api/billing/ive-paid?reference=${data.reference}`, { method: 'POST', credentials: 'include' });
      const json = await res.json(); setStatus(json);
      if (json.active) { alert('Payment confirmed! Pro activated.'); window.location.reload(); } else { alert(json.message); }
    } catch (e: any) { setError(e.message); } finally { setLoading(false); }
  };
  useEffect(() => {
    fetch('/api/billing/me', { credentials: 'include' }).then(r => r.json()).then(json => {
      if (json.billing && json.billing[0]) {
        const latest = json.billing[0];
        if (latest.status === 'pending' || latest.status === 'review') {
          setData({ reference: latest.reference, amount_display: `$${(latest.amount_cents/100).toFixed(2)}`, instructions: { reference: latest.reference } });
          checkStatus(latest.reference);
        }
        if (json.subscription_status === 'active') setStatus({ active: true });
      }
    });
  }, []);
  if (status?.active) {
    return (<div className="p-6 rounded-xl bg-green-900/20 border border-green-700/50 text-green-200"><h3 className="font-bold text-lg">Pro Active</h3><p className="text-sm mt-1">Bank transfer matched via Sovereign Loop.</p></div>);
  }
  return (
    <div className="w-full max-w-2xl mx-auto p-4 md:p-6 rounded-xl bg-slate-900 border border-slate-800">
      <h2 className="text-xl md:text-2xl font-bold text-white">Upgrade to Pro - Bank Transfer</h2>
      <p className="text-sm text-slate-400 mt-2">Sovereign Revenue Loop - no Dodo KYC. Adelaide SA.</p>
      {!data ? (<button onClick={createTransfer} disabled={loading} className="mt-6 w-full md:w-auto px-6 py-3 rounded-lg bg-cyan-500 hover:bg-cyan-400 text-black font-bold">{loading ? 'Creating...' : 'Get Bank Transfer Instructions'}</button>) : (
        <div className="mt-6 space-y-4">
          <div className="p-4 rounded-lg bg-slate-800/80 border border-slate-700">
            <div className="grid grid-cols-1 md:grid-cols-2 gap-3 text-sm">
              <div><span className="text-slate-500">BSB:</span> <span className="text-white font-mono text-lg">{data.instructions.bsb}</span></div>
              <div><span className="text-slate-500">Account:</span> <span className="text-white font-mono text-lg">{data.instructions.account_number}</span></div>
              <div className="md:col-span-2"><span className="text-slate-500">Amount:</span> <span className="text-cyan-300 font-bold text-xl">{data.amount_display}</span></div>
              <div className="md:col-span-2 p-3 rounded bg-black/50 border border-cyan-900/50"><span className="text-slate-500 text-xs uppercase">Your Unique Reference</span><div className="text-cyan-300 font-mono text-2xl md:text-3xl font-bold tracking-wider mt-1 select-all">{data.reference}</div></div>
            </div>
            <p className="text-xs text-amber-300 mt-4 border-t border-slate-700 pt-3">Exact amount + exact reference required.</p>
          </div>
          <div className="flex flex-col md:flex-row gap-3"><button onClick={ivePaid} disabled={loading} className="flex-1 px-6 py-3 rounded-lg bg-white text-black font-bold">I've Paid - Check Activation</button><button onClick={() => checkStatus()} className="px-6 py-3 rounded-lg bg-slate-800 text-white border border-slate-700">Refresh</button></div>
          {status && (<div className="text-sm p-3 rounded bg-slate-800 text-slate-300">Status: <span className="font-bold">{status.status || (status.active ? 'active' : 'pending')}</span></div>)}
        </div>
      )}
      {error && <div className="mt-4 text-sm text-red-400 p-3 rounded bg-red-900/20 border border-red-800">{error}</div>}
    </div>
  );
}
