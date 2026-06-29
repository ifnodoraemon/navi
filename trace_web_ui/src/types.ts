export interface TraceEvent {
  id: string;
  trace_id: string;
  session_id: string;
  run_id: string;
  phase: string;
  source: string;
  peer_id: string;
  sender_id: string;
  tool: string;
  model_role: string;
  ok: boolean;
  input_json: string;
  output_json: string;
  message: string;
  created_at: number;
}

export interface TraceDecision extends TraceEvent {
  decision: any;
}

export interface TraceData {
  events: TraceEvent[];
  runs: any[];
  loop_decisions: TraceDecision[];
}
