export type Target = {
  id: string;
  name: string;
  kind: string;
  available: boolean;
};

export type ReasoningEffort = { id: string; description: string | null };

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
  error?: { code: string; message: string };
};

export type Selection = { model: string | null; reasoningEffort: string | null };

export type ConfirmedSelection = {
  target_id: string;
  target_name: string;
  model: string;
  reasoning_effort: string;
};

export function changeTarget(): Selection {
  return { model: null, reasoningEffort: null };
}

export function applyCapabilities(
  capabilities: Capabilities,
  previous: Selection = { model: null, reasoningEffort: null },
): Selection {
  const model = capabilities.models.find((entry) => entry.model === previous.model)
    ?? capabilities.models.find((entry) => entry.model === capabilities.defaults.model)
    ?? null;
  if (!model) return { model: null, reasoningEffort: null };

  const supported = model.reasoning_efforts.some((entry) => entry.id === previous.reasoningEffort);
  const serverDefault = capabilities.defaults.model === model.model
    ? capabilities.defaults.reasoning_effort
    : null;
  const reasoningEffort = supported
    ? previous.reasoningEffort
    : model.reasoning_efforts.some((entry) => entry.id === serverDefault)
      ? serverDefault
      : model.default_reasoning_effort;
  return { model: model.model, reasoningEffort };
}

export function selectModel(
  capabilities: Capabilities,
  modelName: string,
  previousReasoning: string | null,
): Selection {
  const model = capabilities.models.find((entry) => entry.model === modelName);
  if (!model) return { model: null, reasoningEffort: null };
  const keepPrevious = model.reasoning_efforts.some((entry) => entry.id === previousReasoning);
  return {
    model: model.model,
    reasoningEffort: keepPrevious ? previousReasoning : model.default_reasoning_effort,
  };
}

export function isCurrentGeneration(request: number, current: number): boolean {
  return request === current;
}

export function selectionIsValid(
  capabilities: Capabilities,
  selection: Selection,
): selection is { model: string; reasoningEffort: string } {
  if (!capabilities.target.available || capabilities.error) return false;
  const model = capabilities.models.find((entry) => entry.model === selection.model);
  return Boolean(
    model
    && selection.reasoningEffort
    && model.reasoning_efforts.some((entry) => entry.id === selection.reasoningEffort),
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
} {
  return {
    target_id: targetId,
    model: selection.model,
    reasoning_effort: selection.reasoningEffort,
  };
}
