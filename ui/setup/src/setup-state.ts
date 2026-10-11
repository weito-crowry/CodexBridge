export type Target = {
  id: string;
  name: string;
  kind: string;
  available: boolean;
};

export type ReasoningEffort = { id: string; description: string | null };
export type SandboxMode = "inherit" | "danger-full-access";
export type ApprovalsReviewer = "auto_review" | "user";
export type ExecutionMode = { id: SandboxMode; display_name: string };

export type SetupModel = {
  model: string;
  display_name: string;
  description: string | null;
  reasoning_efforts: ReasoningEffort[];
  default_reasoning_effort: string;
};

export type Capabilities = {
  target: Target;
  models: SetupModel[];
  defaults: { model: string | null; reasoning_effort: string | null };
  execution_modes: ExecutionMode[];
  error?: { code: string; message: string };
};

export type Selection = {
  model: string | null;
  reasoningEffort: string | null;
  sandboxMode: SandboxMode;
  approvalsReviewer: ApprovalsReviewer;
};

export type ConfirmedSelection = {
  target_id: string;
  target_name: string;
  model: string;
  reasoning_effort: string;
  sandbox_mode: SandboxMode;
  approvals_reviewer: ApprovalsReviewer;
};

export function changeTarget(approvalsReviewer: ApprovalsReviewer = "auto_review"): Selection {
  return { model: null, reasoningEffort: null, sandboxMode: "inherit", approvalsReviewer };
}

export function applyCapabilities(
  capabilities: Capabilities,
  previous: Selection = {
    model: null,
    reasoningEffort: null,
    sandboxMode: "inherit",
    approvalsReviewer: "auto_review",
  },
): Selection {
  const sandboxMode = capabilities.execution_modes.some((entry) => entry.id === previous.sandboxMode)
    ? previous.sandboxMode
    : "inherit";
  const model = capabilities.models.find((entry) => entry.model === previous.model)
    ?? capabilities.models.find((entry) => entry.model === capabilities.defaults.model)
    ?? null;
  if (!model) {
    return {
      model: null,
      reasoningEffort: null,
      sandboxMode,
      approvalsReviewer: previous.approvalsReviewer,
    };
  }

  const supported = model.reasoning_efforts.some((entry) => entry.id === previous.reasoningEffort);
  const serverDefault = capabilities.defaults.model === model.model
    ? capabilities.defaults.reasoning_effort
    : null;
  const reasoningEffort = supported
    ? previous.reasoningEffort
    : model.reasoning_efforts.some((entry) => entry.id === serverDefault)
      ? serverDefault
      : model.default_reasoning_effort;
  return { model: model.model, reasoningEffort, sandboxMode, approvalsReviewer: previous.approvalsReviewer };
}

export function selectModel(
  capabilities: Capabilities,
  modelName: string,
  previousReasoning: string | null,
  sandboxMode: SandboxMode = "inherit",
  approvalsReviewer: ApprovalsReviewer = "auto_review",
): Selection {
  const model = capabilities.models.find((entry) => entry.model === modelName);
  if (!model) return { model: null, reasoningEffort: null, sandboxMode, approvalsReviewer };
  const keepPrevious = model.reasoning_efforts.some((entry) => entry.id === previousReasoning);
  return {
    model: model.model,
    reasoningEffort: keepPrevious ? previousReasoning : model.default_reasoning_effort,
    sandboxMode,
    approvalsReviewer,
  };
}

export function isCurrentGeneration(request: number, current: number): boolean {
  return request === current;
}

export function selectionIsValid(
  capabilities: Capabilities,
  selection: Selection,
): selection is {
  model: string;
  reasoningEffort: string;
  sandboxMode: SandboxMode;
  approvalsReviewer: ApprovalsReviewer;
} {
  if (!capabilities.target.available || capabilities.error) return false;
  const model = capabilities.models.find((entry) => entry.model === selection.model);
  return Boolean(
    model
    && selection.reasoningEffort
    && model.reasoning_efforts.some((entry) => entry.id === selection.reasoningEffort)
    && capabilities.execution_modes.some((entry) => entry.id === selection.sandboxMode),
  );
}

export function confirmedContext(selection: ConfirmedSelection): {
  codexbridge_setup: { confirmed: true } & ConfirmedSelection;
} {
  return { codexbridge_setup: { confirmed: true, ...selection } };
}

export function confirmPayload(targetId: string, selection: Selection): {
  target_id: string;
  model: string | null;
  reasoning_effort: string | null;
  sandbox_mode: SandboxMode;
  approvals_reviewer: ApprovalsReviewer;
} {
  return {
    target_id: targetId,
    model: selection.model,
    reasoning_effort: selection.reasoningEffort,
    sandbox_mode: selection.sandboxMode,
    approvals_reviewer: selection.approvalsReviewer,
  };
}
