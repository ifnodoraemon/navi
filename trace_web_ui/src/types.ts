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

export interface TraceRunView {
  id: string;
  trace_id: string;
  parent_run_id: string;
  name: string;
  run_type: string;
  status: string;
  start_time: number;
  end_time: number;
  thread_id: string;
  inputs: any;
  outputs: any;
  tags: string[];
  metadata: any;
}

export interface TraceData {
  events: TraceEvent[];
  runs: TraceRunView[];
  loop_decisions: TraceDecision[];
}

export interface TraceMeta {
  trace_id: string;
  has_error: boolean;
  outcome: string;
  failure_domain: string;
  start_time: number;
  end_time: number;
  duration: number;
  step_count: number;
  preview_text?: string;
}
