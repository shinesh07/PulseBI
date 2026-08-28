"use client";

import React, { useEffect, useState } from "react";
import {
  Activity,
  AlertTriangle,
  ChevronDown,
  Clock,
  Database,
  FileCheck,
  FileCode,
  Layers,
  Lock,
  Play,
  RefreshCw,
  Scale,
  ShieldCheck,
  Sparkles,
  Sliders,
  ThumbsUp,
  ThumbsDown,
  Zap,
} from "lucide-react";

// Types matching FastAPI Backend Exactly
interface HealthData {
  status: string;
  contract_version: number;
  kpis: string[];
  personas: string[];
  fdr_method: string;
  narrative_provider: string;
}

interface Scenario {
  id: string;
  name: string;
  description: string;
  baseline: [string, string];
  event: [string, string];
  baseline_mode: string;
}

interface Finding {
  evidence: {
    kpi: string;
    entity: string;
    label: string;
    decision: "DETECTED" | "LOW_CONFIDENCE" | "ABSTAIN";
    decision_reason: string;
    confidence: number;
    confidence_scale: string;
    materiality: {
      exceedance: number;
      delta_pct: number;
      delta_abs: number;
    };
    statistical_test: {
      tested: boolean;
      p_value: number;
      test_name: string;
      not_tested_reason?: string;
    };
    fdr: {
      adjusted_p_value: number;
      significant: boolean;
    };
    event_window: { start: string; end: string; days: number };
    baseline_window: { start: string; end: string; days: number };
    baseline_mode: string;
    baseline_scale: number;
    baseline_value: number | null;
    current_value: number | null;
    data_quality: { is_complete: boolean; issues: string[] };
    contradicting_evidence: Array<{ check: string; detail: string }>;
    unblock_instructions?: string;
    observed_change: {
      change_type: string;
      describe?: string;
    };
  };
  access: {
    level: "allow" | "mask" | "deny";
    masked_fields: string[];
    reasoning: string;
  };
  narrative: string | null;
  narrative_verified: boolean;
  numerals_checked: number;
  action: {
    driver: string;
    controllable_lever: string;
    action: string;
    expected_impact: string;
    owner: string;
    confidence: number;
    monitoring_plan: string;
  } | null;
  feedback: {
    count: number;
    rating: string;
    multiplier: number;
    describe: string;
  } | null;
}

interface AnalysisData {
  telemetry: {
    total_ms: number;
    deterministic_share: number;
    model_calls: number;
    projected_llm: { total_tokens: number; estimated_cost_usd: number };
  };
  detection: {
    hypotheses_tested: number;
    candidates_evaluated: number;
    raw_significant: number;
    significant_after_correction: number;
    fdr_method: string;
  };
  summary: {
    DETECTED?: number;
    LOW_CONFIDENCE?: number;
    ABSTAIN?: number;
  };
  findings: Finding[];
  reconciliation: {
    status: string;
    evidence_coverage: number;
    checks: Array<{
      label: string;
      status: string;
      detail: string;
      affected_kpis: string[];
    }>;
  };
  freshness: Array<{
    source: string;
    age_hours: number;
    sla_hours: number;
    is_stale: boolean;
  }>;
  persona: string;
  access_audit: Array<{ kpi: string; level: string; reasoning: string }>;
  event_window: [string, string];
}

interface DecompositionData {
  periods?: { prior: string; current: string };
  revenue_waterfall?: {
    prior: number;
    current: number;
    total_variance: number;
    terms: Array<{ name: string; label: string; value: number; explanation: string }>;
    residual: number;
    closes: boolean;
    entering_products: string[];
    exiting_products: string[];
    method: string;
  };
  margin_bridge?: {
    prior_pct: number;
    current_pct: number;
    delta_pp: number;
    contributions: Array<{ factor?: string; name?: string; label: string; value_pp?: number; contribution_pp?: number; explanation: string }>;
    residual_pp: number;
    closes: boolean;
    method: string;
  };
}

interface ColdStartData {
  estimate: {
    entity: string;
    days_observed?: number;
    raw_avg_daily_units: number;
    category_prior_mean: number;
    shrinkage_weight: number;
    empirical_bayes_estimate: number;
  };
  shrinkage_curve: Array<{ day: number; daily_units: number; shrinkage_weight: number; estimate: number }>;
}

export default function Dashboard() {
  const [health, setHealth] = useState<HealthData | null>(null);
  const [scenarios, setScenarios] = useState<Scenario[]>([]);
  const [selectedScenario, setSelectedScenario] = useState<string>("");
  const [selectedPersona, setSelectedPersona] = useState<string>("CFO_EXECUTIVE");
  const [baselineStart, setBaselineStart] = useState<string>("");
  const [baselineEnd, setBaselineEnd] = useState<string>("");
  const [eventStart, setEventStart] = useState<string>("");
  const [eventEnd, setEventEnd] = useState<string>("");
  const [baselineMode, setBaselineMode] = useState<string>("MATCHED_LENGTH");

  const [loading, setLoading] = useState<boolean>(false);
  const [error, setError] = useState<string | null>(null);
  const [analysis, setAnalysis] = useState<AnalysisData | null>(null);
  const [decomposition, setDecomposition] = useState<DecompositionData | null>(null);
  const [coldStart, setColdStart] = useState<ColdStartData | null>(null);

  const [filterTab, setFilterTab] = useState<"ALL" | "DETECTED" | "LOW_CONFIDENCE" | "ABSTAIN">("ALL");
  const [expandedDrawers, setExpandedDrawers] = useState<Record<number, boolean>>({});
  const [expandedActions, setExpandedActions] = useState<Record<number, boolean>>({});
  const [votingFeedback, setVotingFeedback] = useState<Record<string, string>>({});
  const [darkMode, setDarkMode] = useState<boolean>(true);

  // Boot: fetch health & scenarios
  useEffect(() => {
    async function init() {
      try {
        const [hRes, sRes] = await Promise.all([
          fetch("/api/health"),
          fetch("/api/scenarios"),
        ]);
        if (!hRes.ok || !sRes.ok) throw new Error("Backend server unavailable");
        const hData: HealthData = await hRes.json();
        const sData = await sRes.json();

        setHealth(hData);
        setScenarios(sData.scenarios);

        if (sData.scenarios.length > 0) {
          const first = sData.scenarios[0];
          setSelectedScenario(first.id);
          setBaselineStart(first.baseline[0]);
          setBaselineEnd(first.baseline[1]);
          setEventStart(first.event[0]);
          setEventEnd(first.event[1]);
          setBaselineMode(first.baseline_mode);
        }
      } catch (err: any) {
        setError(err.message || "Failed to connect to PulseBI backend");
      }
    }
    init();
  }, []);

  // Handle Scenario Change
  const handleScenarioChange = (scenId: string) => {
    setSelectedScenario(scenId);
    const scen = scenarios.find((s) => s.id === scenId);
    if (scen) {
      setBaselineStart(scen.baseline[0]);
      setBaselineEnd(scen.baseline[1]);
      setEventStart(scen.event[0]);
      setEventEnd(scen.event[1]);
      setBaselineMode(scen.baseline_mode);
    }
  };

  // Run Analysis Pipeline
  const runAnalysis = async () => {
    if (!baselineStart || !baselineEnd || !eventStart || !eventEnd) return;
    setLoading(true);
    setError(null);
    try {
      const q = new URLSearchParams({
        persona: selectedPersona,
        baseline_start: baselineStart,
        baseline_end: baselineEnd,
        event_start: eventStart,
        event_end: eventEnd,
        baseline_mode: baselineMode,
      });

      const priorMonth = baselineStart.slice(0, 7) || "2023-10";
      const currentMonth = eventStart.slice(0, 7) || "2023-11";

      const [aRes, dRes, cListRes] = await Promise.all([
        fetch(`/api/analyse?${q}`),
        fetch(`/api/decomposition?prior_period=${priorMonth}&current_period=${currentMonth}`),
        fetch("/api/cold-start"),
      ]);

      if (!aRes.ok) {
        const errJson = await aRes.json();
        throw new Error(errJson.detail || "Analysis pipeline error");
      }

      const aData: AnalysisData = await aRes.json();
      setAnalysis(aData);

      if (dRes.ok) {
        const dData: DecompositionData = await dRes.json();
        setDecomposition(dData);
      }

      if (cListRes.ok) {
        const cListData = await cListRes.json();
        if (cListData.candidates && cListData.candidates.length > 0) {
          const coldEntity = cListData.candidates[0].entity;
          const cSingleRes = await fetch(`/api/cold-start/${coldEntity}`);
          if (cSingleRes.ok) {
            const cSingleData: ColdStartData = await cSingleRes.json();
            setColdStart(cSingleData);
          }
        }
      }
    } catch (err: any) {
      setError(err.message || "Failed to execute pipeline");
    } finally {
      setLoading(false);
    }
  };

  // Auto-run analysis when scenarios load
  useEffect(() => {
    if (scenarios.length > 0 && selectedScenario) {
      runAnalysis();
    }
  }, [scenarios]);

  // Analyst Feedback Vote
  const handleVote = async (kpi: string, entity: string, rating: "UPVOTE" | "DOWNVOTE") => {
    const key = `${kpi}-${entity}`;
    setVotingFeedback((prev) => ({ ...prev, [key]: rating }));
    try {
      await fetch("/api/feedback", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ persona: selectedPersona, kpi, entity, rating }),
      });
      runAnalysis();
    } catch (e) {
      console.error("Feedback error", e);
    }
  };

  const toggleDrawer = (idx: number) => {
    setExpandedDrawers((prev) => ({ ...prev, [idx]: !prev[idx] }));
  };

  const toggleAction = (idx: number) => {
    setExpandedActions((prev) => ({ ...prev, [idx]: !prev[idx] }));
  };

  const activeScenarioObj = scenarios.find((s) => s.id === selectedScenario);

  const filteredFindings = (analysis?.findings || []).filter((f) => {
    if (filterTab === "ALL") return true;
    return f.evidence.decision === filterTab;
  });

  const verifiedCount = (analysis?.findings || []).filter((f) => f.narrative_verified).length;
  const totalNumerals = (analysis?.findings || []).reduce((a, f) => a + f.numerals_checked, 0);

  const pvmWaterfall = decomposition?.revenue_waterfall;
  const marginBridge = decomposition?.margin_bridge;

  return (
    <div className={`min-h-screen transition-colors duration-200 ${darkMode ? "dark bg-[#080d14] text-slate-100" : "bg-slate-50 text-slate-900"}`}>
      {/* Header */}
      <header className="sticky top-0 z-40 border-b border-slate-800/60 bg-[#0b131e]/80 backdrop-blur-md">
        <div className="mx-auto flex max-w-7xl items-center justify-between px-4 py-3 sm:px-6">
          <div className="flex items-center gap-3">
            <div className="flex h-10 w-10 items-center justify-center rounded-xl bg-gradient-to-tr from-cyan-600 to-blue-600 shadow-lg shadow-cyan-500/20">
              <Activity className="h-5.5 w-5.5 text-white" />
            </div>
            <div>
              <div className="flex items-center gap-2">
                <h1 className="font-mono text-xl font-black tracking-tight text-white">PulseBI</h1>
                <span className="rounded-full bg-cyan-500/10 px-2.5 py-0.5 font-mono text-[10px] font-semibold text-cyan-400 border border-cyan-500/20">
                  GOVERNED ENGINE v2.0
                </span>
              </div>
              <p className="text-xs text-slate-400">Deterministic SQL & Statistical Intelligence-to-Action</p>
            </div>
          </div>

          <div className="flex items-center gap-3">
            {health && (
              <div className="hidden md:flex items-center gap-3 rounded-lg border border-slate-800 bg-slate-900/60 px-3 py-1.5 font-mono text-xs text-slate-300">
                <span className="flex items-center gap-1.5 text-emerald-400">
                  <span className="relative flex h-2 w-2">
                    <span className="animate-ping absolute inline-flex h-full w-full rounded-full bg-emerald-400 opacity-75"></span>
                    <span className="relative inline-flex rounded-full h-2 w-2 bg-emerald-500"></span>
                  </span>
                  {health.fdr_method.replace(/_/g, "-")}
                </span>
                <span className="text-slate-600">|</span>
                <span className="text-cyan-400">Provider: {health.narrative_provider}</span>
              </div>
            )}
            <button
              onClick={() => setDarkMode(!darkMode)}
              className="flex h-9 w-9 items-center justify-center rounded-lg border border-slate-800 bg-slate-900 text-slate-400 hover:text-slate-100 transition-colors"
            >
              {darkMode ? "☀️" : "🌙"}
            </button>
          </div>
        </div>
      </header>

      {/* Main Container */}
      <main className="mx-auto max-w-7xl px-4 py-6 sm:px-6 space-y-6">
        
        {/* Interactive Control Panel */}
        <section className="rounded-2xl border border-slate-800/80 bg-gradient-to-b from-[#111a28] to-[#0d1420] p-5 shadow-xl">
          <div className="flex items-center justify-between pb-4 border-b border-slate-800">
            <div className="flex items-center gap-2 font-mono text-xs uppercase tracking-wider text-cyan-400 font-semibold">
              <Sliders className="h-4 w-4" /> Scenario & Governance Workbench
            </div>
            {activeScenarioObj && (
              <p className="text-xs text-slate-400 italic max-w-xl text-right">
                {activeScenarioObj.description}
              </p>
            )}
          </div>

          <div className="mt-4 grid grid-cols-1 gap-4 sm:grid-cols-2 lg:grid-cols-6">
            <div className="space-y-1.5 lg:col-span-2">
              <label className="font-mono text-[11px] font-semibold uppercase text-slate-400">Scenario Preset</label>
              <div className="relative">
                <select
                  value={selectedScenario}
                  onChange={(e) => handleScenarioChange(e.target.value)}
                  className="w-full appearance-none rounded-xl border border-slate-700 bg-slate-900 px-3.5 py-2.5 font-mono text-xs font-medium text-slate-100 focus:border-cyan-500 focus:outline-none"
                >
                  {scenarios.map((s) => (
                    <option key={s.id} value={s.id}>
                      {s.name}
                    </option>
                  ))}
                </select>
                <ChevronDown className="pointer-events-none absolute right-3 top-3 h-4 w-4 text-slate-400" />
              </div>
            </div>

            <div className="space-y-1.5 lg:col-span-2">
              <label className="font-mono text-[11px] font-semibold uppercase text-slate-400">Persona Role</label>
              <div className="relative">
                <select
                  value={selectedPersona}
                  onChange={(e) => setSelectedPersona(e.target.value)}
                  className="w-full appearance-none rounded-xl border border-slate-700 bg-slate-900 px-3.5 py-2.5 font-mono text-xs font-medium text-slate-100 focus:border-cyan-500 focus:outline-none"
                >
                  {health?.personas.map((p) => (
                    <option key={p} value={p}>
                      {p.replace(/_/g, " ")}
                    </option>
                  ))}
                </select>
                <ChevronDown className="pointer-events-none absolute right-3 top-3 h-4 w-4 text-slate-400" />
              </div>
            </div>

            <div className="space-y-1.5 lg:col-span-1">
              <label className="font-mono text-[11px] font-semibold uppercase text-slate-400">Baseline Mode</label>
              <select
                value={baselineMode}
                onChange={(e) => setBaselineMode(e.target.value)}
                className="w-full appearance-none rounded-xl border border-slate-700 bg-slate-900 px-3 py-2.5 font-mono text-xs text-slate-100 focus:border-cyan-500 focus:outline-none"
              >
                <option value="AS_REPORTED">As Reported</option>
                <option value="MATCHED_LENGTH">Matched Length</option>
              </select>
            </div>

            <div className="flex items-end lg:col-span-1">
              <button
                onClick={runAnalysis}
                disabled={loading}
                className="flex w-full items-center justify-center gap-2 rounded-xl bg-gradient-to-r from-cyan-500 to-blue-600 py-2.5 px-4 font-mono text-xs font-bold text-white shadow-lg shadow-cyan-500/25 transition-all hover:brightness-110 active:scale-95 disabled:opacity-50"
              >
                {loading ? (
                  <RefreshCw className="h-4 w-4 animate-spin" />
                ) : (
                  <>
                    <Play className="h-4 w-4 fill-current" /> Analyse
                  </>
                )}
              </button>
            </div>
          </div>

          <div className="mt-4 grid grid-cols-2 gap-3 sm:grid-cols-4 pt-3 border-t border-slate-800/60 font-mono text-xs">
            <div>
              <span className="text-[10px] text-slate-500 uppercase block">Baseline From</span>
              <input
                type="date"
                value={baselineStart}
                onChange={(e) => setBaselineStart(e.target.value)}
                className="w-full rounded-lg border border-slate-800 bg-slate-950 px-2.5 py-1 text-slate-200 focus:border-cyan-500"
              />
            </div>
            <div>
              <span className="text-[10px] text-slate-500 uppercase block">Baseline To</span>
              <input
                type="date"
                value={baselineEnd}
                onChange={(e) => setBaselineEnd(e.target.value)}
                className="w-full rounded-lg border border-slate-800 bg-slate-950 px-2.5 py-1 text-slate-200 focus:border-cyan-500"
              />
            </div>
            <div>
              <span className="text-[10px] text-slate-500 uppercase block">Event From</span>
              <input
                type="date"
                value={eventStart}
                onChange={(e) => setEventStart(e.target.value)}
                className="w-full rounded-lg border border-slate-800 bg-slate-950 px-2.5 py-1 text-slate-200 focus:border-cyan-500"
              />
            </div>
            <div>
              <span className="text-[10px] text-slate-500 uppercase block">Event To</span>
              <input
                type="date"
                value={eventEnd}
                onChange={(e) => setEventEnd(e.target.value)}
                className="w-full rounded-lg border border-slate-800 bg-slate-950 px-2.5 py-1 text-slate-200 focus:border-cyan-500"
              />
            </div>
          </div>
        </section>

        {/* Error Notification */}
        {error && (
          <div className="rounded-xl border border-rose-500/30 bg-rose-500/10 p-4 font-mono text-xs text-rose-400 flex items-center gap-3">
            <AlertTriangle className="h-5 w-5 shrink-0" />
            <span>{error}</span>
          </div>
        )}

        {/* Telemetry Stat Cards */}
        {analysis && (
          <section className="grid grid-cols-2 gap-3 sm:grid-cols-3 lg:grid-cols-6">
            <div className="rounded-xl border border-emerald-500/20 bg-emerald-500/5 p-3.5 backdrop-blur-sm">
              <span className="font-mono text-[10px] uppercase font-semibold text-emerald-400 block mb-1">Detected Movements</span>
              <div className="flex items-baseline justify-between">
                <span className="font-mono text-2xl font-black text-emerald-400">{analysis.summary?.DETECTED || 0}</span>
                <span className="text-[10px] font-mono text-slate-400">{analysis.summary?.LOW_CONFIDENCE || 0} low conf</span>
              </div>
            </div>

            <div className="rounded-xl border border-rose-500/20 bg-rose-500/5 p-3.5 backdrop-blur-sm">
              <span className="font-mono text-[10px] uppercase font-semibold text-rose-400 block mb-1">Abstained</span>
              <div className="flex items-baseline justify-between">
                <span className="font-mono text-2xl font-black text-rose-400">{analysis.summary?.ABSTAIN || 0}</span>
                <span className="text-[10px] font-mono text-slate-400">insufficient data</span>
              </div>
            </div>

            <div className="rounded-xl border border-slate-800 bg-slate-900/60 p-3.5">
              <span className="font-mono text-[10px] uppercase font-semibold text-slate-400 block mb-1">Hypotheses Tested</span>
              <div className="flex items-baseline justify-between">
                <span className="font-mono text-2xl font-black text-slate-100">{analysis.detection?.hypotheses_tested || 0}</span>
                <span className="text-[10px] font-mono text-slate-400">of {analysis.detection?.candidates_evaluated || 0} candidates</span>
              </div>
            </div>

            <div className="rounded-xl border border-slate-800 bg-slate-900/60 p-3.5">
              <span className="font-mono text-[10px] uppercase font-semibold text-slate-400 block mb-1">Raw → Corrected</span>
              <div className="flex items-baseline justify-between">
                <span className="font-mono text-2xl font-black text-amber-400">
                  {analysis.detection?.raw_significant || 0} → {analysis.detection?.significant_after_correction || 0}
                </span>
                <span className="text-[10px] font-mono text-slate-400">{(analysis.detection?.fdr_method || "").replace(/_/g, "-")}</span>
              </div>
            </div>

            <div className="rounded-xl border border-cyan-500/20 bg-cyan-500/5 p-3.5">
              <span className="font-mono text-[10px] uppercase font-semibold text-cyan-400 block mb-1">Numerals Verified</span>
              <div className="flex items-baseline justify-between">
                <span className="font-mono text-2xl font-black text-cyan-300">{totalNumerals}</span>
                <span className="text-[10px] font-mono text-cyan-400">{verifiedCount}/{(analysis.findings || []).length} narratives</span>
              </div>
            </div>

            <div className="rounded-xl border border-indigo-500/20 bg-indigo-500/5 p-3.5">
              <span className="font-mono text-[10px] uppercase font-semibold text-indigo-400 block mb-1">Execution Latency</span>
              <div className="flex items-baseline justify-between">
                <span className="font-mono text-2xl font-black text-indigo-300">{(analysis.telemetry?.total_ms || 0).toFixed(0)}ms</span>
                <span className="text-[10px] font-mono text-slate-400">{((analysis.telemetry?.deterministic_share || 1) * 100).toFixed(0)}% SQL/Stats</span>
              </div>
            </div>
          </section>
        )}

        {/* Multi-Factor Decomposition Section */}
        {pvmWaterfall && marginBridge && (
          <section className="grid grid-cols-1 gap-6 lg:grid-cols-2">
            {/* PVM Revenue Waterfall */}
            <div className="rounded-2xl border border-slate-800 bg-[#0e1623] p-5 shadow-lg space-y-4">
              <div className="flex items-center justify-between border-b border-slate-800 pb-3">
                <div>
                  <h3 className="font-mono text-sm font-bold text-slate-100 flex items-center gap-2">
                    <Layers className="h-4 w-4 text-cyan-400" /> Revenue PVM Waterfall
                  </h3>
                  <p className="text-xs text-slate-400">Exact 5-term Price-Volume-Mix decomposition</p>
                </div>
                {pvmWaterfall.closes && (
                  <span className="rounded-full bg-emerald-500/10 px-2.5 py-0.5 font-mono text-[10px] font-semibold text-emerald-400 border border-emerald-500/20">
                    EXACT CLOSURE ✓
                  </span>
                )}
              </div>

              <div className="space-y-3 font-mono text-xs">
                {(pvmWaterfall.terms || []).map((term) => {
                  const val = term.value || 0;
                  const isPos = val >= 0;
                  const maxVal = Math.max(...(pvmWaterfall.terms || []).map((t) => Math.abs(t.value || 0)), 1);
                  const barWidth = Math.min((Math.abs(val) / maxVal) * 100, 100);

                  return (
                    <div key={term.name} className="space-y-1">
                      <div className="flex justify-between text-slate-300">
                        <span className="font-semibold">{term.label}</span>
                        <span className={`font-bold ${isPos ? "text-emerald-400" : "text-rose-400"}`}>
                          {isPos ? "+" : ""}${val.toLocaleString(undefined, { minimumFractionDigits: 2, maximumFractionDigits: 2 })}
                        </span>
                      </div>
                      <div className="relative h-3 w-full rounded-full bg-slate-900 overflow-hidden border border-slate-800">
                        <div
                          className={`h-full rounded-full transition-all duration-500 ${isPos ? "bg-emerald-500" : "bg-rose-500"}`}
                          style={{ width: `${barWidth}%` }}
                        />
                      </div>
                      <p className="text-[10px] text-slate-500">{term.explanation}</p>
                    </div>
                  );
                })}
              </div>
            </div>

            {/* Shapley Margin Bridge */}
            <div className="rounded-2xl border border-slate-800 bg-[#0e1623] p-5 shadow-lg space-y-4">
              <div className="flex items-center justify-between border-b border-slate-800 pb-3">
                <div>
                  <h3 className="font-mono text-sm font-bold text-slate-100 flex items-center gap-2">
                    <Scale className="h-4 w-4 text-cyan-400" /> Shapley Gross Margin Bridge
                  </h3>
                  <p className="text-xs text-slate-400">Non-additive ratio decomposition across 32 counterfactual states</p>
                </div>
                {marginBridge.closes && (
                  <span className="rounded-full bg-emerald-500/10 px-2.5 py-0.5 font-mono text-[10px] font-semibold text-emerald-400 border border-emerald-500/20">
                    EXACT CLOSURE ✓
                  </span>
                )}
              </div>

              <div className="space-y-3 font-mono text-xs">
                {(marginBridge.contributions || []).map((factor) => {
                  const val = factor.value_pp ?? factor.contribution_pp ?? 0;
                  const isPos = val >= 0;
                  const maxVal = Math.max(...(marginBridge.contributions || []).map((f) => Math.abs(f.value_pp ?? f.contribution_pp ?? 0)), 0.1);
                  const barWidth = Math.min((Math.abs(val) / maxVal) * 100, 100);

                  return (
                    <div key={factor.factor || factor.name || factor.label} className="space-y-1">
                      <div className="flex justify-between text-slate-300">
                        <span className="font-semibold">{factor.label}</span>
                        <span className={`font-bold ${isPos ? "text-emerald-400" : "text-rose-400"}`}>
                          {isPos ? "+" : ""}{val.toFixed(2)} pp
                        </span>
                      </div>
                      <div className="relative h-3 w-full rounded-full bg-slate-900 overflow-hidden border border-slate-800">
                        <div
                          className={`h-full rounded-full transition-all duration-500 ${isPos ? "bg-emerald-500" : "bg-rose-500"}`}
                          style={{ width: `${barWidth}%` }}
                        />
                      </div>
                      <p className="text-[10px] text-slate-500">{factor.explanation}</p>
                    </div>
                  );
                })}
              </div>
            </div>
          </section>
        )}

        {/* Findings Feed Section */}
        <section className="space-y-4">
          <div className="flex flex-col sm:flex-row items-start sm:items-center justify-between gap-4 border-b border-slate-800 pb-3">
            <div>
              <h2 className="font-mono text-lg font-bold text-slate-100 flex items-center gap-2">
                <FileCheck className="h-5 w-5 text-cyan-400" /> Persona Findings & Verified Insights
              </h2>
              <p className="text-xs text-slate-400">
                Persona: <strong className="text-cyan-300">{selectedPersona.replace(/_/g, " ")}</strong> · Output gated by Faithfulness Verifier
              </p>
            </div>

            <div className="flex items-center gap-1.5 rounded-xl border border-slate-800 bg-slate-900 p-1 font-mono text-xs">
              {(["ALL", "DETECTED", "LOW_CONFIDENCE", "ABSTAIN"] as const).map((tab) => (
                <button
                  key={tab}
                  onClick={() => setFilterTab(tab)}
                  className={`rounded-lg px-3 py-1.5 font-semibold transition-all ${
                    filterTab === tab
                      ? "bg-cyan-500/20 text-cyan-300 border border-cyan-500/30"
                      : "text-slate-400 hover:text-slate-200"
                  }`}
                >
                  {tab.replace(/_/g, " ")}
                </button>
              ))}
            </div>
          </div>

          <div className="space-y-4">
            {filteredFindings.length === 0 ? (
              <div className="rounded-xl border border-slate-800 bg-slate-900/40 p-8 text-center font-mono text-xs text-slate-500">
                No findings match the current filter or persona permissions.
              </div>
            ) : (
              filteredFindings.map((finding, idx) => {
                const ev = finding.evidence;
                const dec = ev.decision;
                const isMasked = finding.access.level === "mask";
                const isAbstain = dec === "ABSTAIN";
                const isLow = dec === "LOW_CONFIDENCE";

                let borderCls = "border-emerald-500/40 bg-emerald-950/10";
                let badgeCls = "bg-emerald-500/10 text-emerald-400 border-emerald-500/20";

                if (isLow) {
                  borderCls = "border-amber-500/40 bg-amber-950/10";
                  badgeCls = "bg-amber-500/10 text-amber-400 border-amber-500/20";
                } else if (isAbstain) {
                  borderCls = "border-rose-500/40 bg-rose-950/10";
                  badgeCls = "bg-rose-500/10 text-rose-400 border-rose-500/20";
                }

                const voteKey = `${ev.kpi}-${ev.entity}`;
                const currentVote = votingFeedback[voteKey] || finding.feedback?.rating;

                return (
                  <div key={idx} className={`rounded-2xl border p-5 backdrop-blur-md transition-all ${borderCls}`}>
                    <div className="flex flex-wrap items-center justify-between gap-2 pb-3 border-b border-slate-800/60">
                      <div className="flex items-center gap-2">
                        <span className="font-mono text-sm font-bold text-slate-100">
                          {ev.label} · <span className="text-cyan-400">{ev.entity}</span>
                        </span>
                        <span className={`rounded-full px-2.5 py-0.5 font-mono text-[10px] font-bold uppercase border ${badgeCls}`}>
                          {dec.replace(/_/g, " ")}
                        </span>
                        {isMasked && (
                          <span className="rounded-full bg-slate-800 px-2 py-0.5 font-mono text-[10px] font-semibold text-slate-400 border border-slate-700 flex items-center gap-1">
                            <Lock className="h-3 w-3 text-slate-400" /> MASKED
                          </span>
                        )}
                      </div>

                      <span className="font-mono text-xs font-bold text-slate-200">
                        {ev.observed_change.describe || "Observed Movement"}
                      </span>
                    </div>

                    <div className="my-3 rounded-xl border border-slate-800 bg-slate-950/60 p-4">
                      {finding.narrative ? (
                        <p className="text-sm leading-relaxed text-slate-200">{finding.narrative}</p>
                      ) : (
                        <p className="font-mono text-xs text-rose-400 italic">
                          ⚠️ Narrative withheld: a numeral failed faithfulness verification.
                        </p>
                      )}
                    </div>

                    <div className="flex flex-wrap items-center gap-x-6 gap-y-2 font-mono text-xs text-slate-400 pt-1">
                      <span>
                        Confidence: <strong className="text-slate-200">{(ev.confidence || 0).toFixed(3)}</strong>
                      </span>
                      <span>
                        Materiality: <strong className="text-slate-200">{(ev.materiality?.exceedance || 0).toFixed(1)}×</strong>
                      </span>
                      <span>
                        {ev.statistical_test?.tested
                          ? `Adj p: ${(ev.fdr?.adjusted_p_value || 0).toExponential(2)}`
                          : `No test: ${ev.statistical_test?.not_tested_reason}`}
                      </span>
                      <span className="flex items-center gap-1 text-cyan-400">
                        <ShieldCheck className="h-3.5 w-3.5" />
                        {finding.numerals_checked} Numerals Verified
                      </span>
                    </div>

                    <div className="mt-4 flex flex-wrap items-center justify-between gap-3 pt-3 border-t border-slate-800/60">
                      <div className="flex items-center gap-3 font-mono text-xs">
                        <button
                          onClick={() => toggleDrawer(idx)}
                          className="flex items-center gap-1 text-cyan-400 hover:text-cyan-300 font-semibold"
                        >
                          <FileCode className="h-3.5 w-3.5" />
                          {expandedDrawers[idx] ? "Hide Evidence JSON" : "View Evidence Ledger"}
                        </button>
                        {finding.action && (
                          <button
                            onClick={() => toggleAction(idx)}
                            className="flex items-center gap-1 text-indigo-400 hover:text-indigo-300 font-semibold"
                          >
                            <Zap className="h-3.5 w-3.5" />
                            {expandedActions[idx] ? "Hide Recommended Action" : "View Recommended Action"}
                          </button>
                        )}
                      </div>

                      <div className="flex items-center gap-2 font-mono text-xs">
                        <span className="text-slate-500 text-[10px]">Rank Relevance:</span>
                        <button
                          onClick={() => handleVote(ev.kpi, ev.entity, "UPVOTE")}
                          className={`rounded-lg border px-2.5 py-1 transition-all ${
                            currentVote === "UPVOTE"
                              ? "border-cyan-500 bg-cyan-500/20 text-cyan-300"
                              : "border-slate-800 bg-slate-900 text-slate-400 hover:text-slate-200"
                          }`}
                        >
                          <ThumbsUp className="h-3 w-3 inline mr-1" /> Useful
                        </button>
                        <button
                          onClick={() => handleVote(ev.kpi, ev.entity, "DOWNVOTE")}
                          className={`rounded-lg border px-2.5 py-1 transition-all ${
                            currentVote === "DOWNVOTE"
                              ? "border-rose-500 bg-rose-500/20 text-rose-300"
                              : "border-slate-800 bg-slate-900 text-slate-400 hover:text-slate-200"
                          }`}
                        >
                          <ThumbsDown className="h-3 w-3 inline mr-1" /> Less
                        </button>
                      </div>
                    </div>

                    {expandedActions[idx] && finding.action && (
                      <div className="mt-3 rounded-xl border border-indigo-500/30 bg-indigo-950/20 p-4 space-y-2 font-mono text-xs">
                        <div className="flex items-center justify-between text-indigo-300 font-bold border-b border-indigo-500/20 pb-2">
                          <span>RECOMMENDED ACTION: {finding.action.controllable_lever}</span>
                          <span>Owner: {finding.action.owner}</span>
                        </div>
                        <p className="text-slate-200 font-sans text-sm">{finding.action.action}</p>
                        <div className="grid grid-cols-2 gap-4 text-slate-400 pt-2 border-t border-indigo-500/10">
                          <div>
                            <span className="text-indigo-400 font-bold block">Expected Impact:</span>
                            {finding.action.expected_impact}
                          </div>
                          <div>
                            <span className="text-indigo-400 font-bold block">Monitoring Plan:</span>
                            {finding.action.monitoring_plan}
                          </div>
                        </div>
                      </div>
                    )}

                    {expandedDrawers[idx] && (
                      <div className="mt-3 rounded-xl border border-slate-800 bg-slate-950 p-4 font-mono text-xs">
                        <pre className="overflow-x-auto text-slate-300 text-[11px] leading-relaxed">
                          {JSON.stringify(
                            {
                              decision_reason: ev.decision_reason,
                              window: `${ev.event_window?.start} → ${ev.event_window?.end} (${ev.event_window?.days}d)`,
                              baseline: `${ev.baseline_window?.days}d, mode ${ev.baseline_mode}, scale ${(ev.baseline_scale || 0).toFixed(4)}`,
                              baseline_value: ev.baseline_value,
                              current_value: ev.current_value,
                              statistical_test: ev.statistical_test,
                              fdr: ev.fdr,
                              data_quality: ev.data_quality,
                              contradicting_evidence: ev.contradicting_evidence,
                              unblock: ev.unblock_instructions,
                              access: finding.access,
                            },
                            null,
                            2
                          )}
                        </pre>
                      </div>
                    )}
                  </div>
                );
              })
            )}
          </div>
        </section>

        {/* Reconciliation & Freshness Tables */}
        {analysis && (
          <section className="grid grid-cols-1 gap-6 lg:grid-cols-2">
            <div className="rounded-2xl border border-slate-800 bg-[#0e1623] p-5 shadow-lg space-y-3">
              <h3 className="font-mono text-sm font-bold text-slate-100 flex items-center gap-2 border-b border-slate-800 pb-2">
                <Database className="h-4 w-4 text-cyan-400" /> Cross-Source Reconciliation
              </h3>
              <div className="overflow-x-auto">
                <table className="w-full text-left font-mono text-xs">
                  <thead>
                    <tr className="border-b border-slate-800 text-slate-400">
                      <th className="py-2 px-2">Status</th>
                      <th className="py-2 px-2">Check</th>
                      <th className="py-2 px-2">Detail</th>
                    </tr>
                  </thead>
                  <tbody className="divide-y divide-slate-800/60">
                    {(analysis.reconciliation?.checks || []).map((chk, i) => (
                      <tr key={i} className="text-slate-300">
                        <td className="py-2 px-2">
                          <span
                            className={`rounded px-1.5 py-0.5 text-[10px] font-bold ${
                              chk.status === "CONSISTENT"
                                ? "bg-emerald-500/10 text-emerald-400"
                                : chk.status === "CONTRADICTORY"
                                ? "bg-rose-500/10 text-rose-400"
                                : "bg-amber-500/10 text-amber-400"
                            }`}
                          >
                            {chk.status}
                          </span>
                        </td>
                        <td className="py-2 px-2 font-semibold text-slate-200">{chk.label}</td>
                        <td className="py-2 px-2 text-slate-400">{chk.detail}</td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            </div>

            <div className="rounded-2xl border border-slate-800 bg-[#0e1623] p-5 shadow-lg space-y-3">
              <h3 className="font-mono text-sm font-bold text-slate-100 flex items-center gap-2 border-b border-slate-800 pb-2">
                <Clock className="h-4 w-4 text-cyan-400" /> Source Freshness & SLAs
              </h3>
              <div className="overflow-x-auto">
                <table className="w-full text-left font-mono text-xs">
                  <thead>
                    <tr className="border-b border-slate-800 text-slate-400">
                      <th className="py-2 px-2">Source</th>
                      <th className="py-2 px-2">Age</th>
                      <th className="py-2 px-2">SLA</th>
                      <th className="py-2 px-2">Status</th>
                    </tr>
                  </thead>
                  <tbody className="divide-y divide-slate-800/60">
                    {(analysis.freshness || []).map((f, i) => (
                      <tr key={i} className="text-slate-300">
                        <td className="py-2 px-2 font-bold text-slate-200">{f.source}</td>
                        <td className="py-2 px-2">{f.age_hours}h</td>
                        <td className="py-2 px-2">{f.sla_hours}h</td>
                        <td className="py-2 px-2">
                          <span
                            className={`rounded px-1.5 py-0.5 text-[10px] font-bold ${
                              f.is_stale ? "bg-amber-500/10 text-amber-400" : "bg-emerald-500/10 text-emerald-400"
                            }`}
                          >
                            {f.is_stale ? "STALE" : "FRESH"}
                          </span>
                        </td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            </div>
          </section>
        )}

        {/* Cold Start Shrinkage Curve */}
        {coldStart && coldStart.estimate && (
          <section className="rounded-2xl border border-slate-800 bg-[#0e1623] p-5 shadow-lg space-y-3">
            <h3 className="font-mono text-sm font-bold text-slate-100 flex items-center gap-2 border-b border-slate-800 pb-2">
              <Sparkles className="h-4 w-4 text-cyan-400" /> Empirical Bayes Cold-Start Shrinkage Curve ({coldStart.estimate.entity})
            </h3>
            <p className="text-xs text-slate-400">
              New SKU with sparse history borrows strength from category prior (Observed Days: {coldStart.estimate.days_observed || 0}, Prior Mean: {(coldStart.estimate.category_prior_mean || 0).toFixed(1)} units)
            </p>
            <div className="space-y-1.5 font-mono text-xs pt-2">
              {(coldStart.shrinkage_curve || []).map((pt, idx) => (
                <div key={`shrinkage-${pt.day ?? idx}-${idx}`} className="grid grid-cols-12 items-center gap-2 text-slate-300">
                  <span className="col-span-2 text-slate-400">Day {pt.day}</span>
                  <div className="col-span-8 h-3 rounded-full bg-slate-900 overflow-hidden border border-slate-800">
                    <div
                      className="h-full bg-cyan-500 transition-all duration-300"
                      style={{ width: `${(pt.shrinkage_weight || 0) * 100}%` }}
                    />
                  </div>
                  <span className="col-span-2 text-right font-bold text-cyan-400 font-mono">
                    {((pt.shrinkage_weight || 0) * 100).toFixed(0)}% Prior
                  </span>
                </div>
              ))}
            </div>
          </section>
        )}

      </main>

      <footer className="mt-12 border-t border-slate-800/80 bg-[#070b10] py-6 text-center font-mono text-xs text-slate-500">
        PulseBI · Contract-Driven Semantic Architecture · Machine-Verified Faithfulness Gate · 100% Deterministic SQL/Stats
      </footer>
    </div>
  );
}
