import { App, PostMessageTransport } from "@modelcontextprotocol/ext-apps";
import "./style.css";
import {
  applyCapabilities,
  changeTarget,
  confirmPayload,
  confirmedContext,
  isCurrentGeneration,
  selectModel,
  selectionIsValid,
  type Capabilities,
  type Selection,
  type SetupModel,
  type Target,
} from "./setup-state";

type SetupResult = {
  targets: Target[];
  selection_required: boolean;
  capabilities?: Capabilities;
};

type ConfirmResult = {
  confirmed: true;
  selection: {
    target_id: string;
    target_name: string;
    model: string;
    reasoning_effort: string;
  };
};

const app = new App({ name: "CodexBridge Setup", version: "0.1.0" }, {});
const status = document.querySelector<HTMLElement>("#status")!;
const targetSelect = document.querySelector<HTMLSelectElement>("#target")!;
const modelSelect = document.querySelector<HTMLSelectElement>("#model")!;
const reasoningSelect = document.querySelector<HTMLSelectElement>("#reasoning")!;
const modelDescription = document.querySelector<HTMLElement>("#model-description")!;
const reasoningDescription = document.querySelector<HTMLElement>("#reasoning-description")!;
const refreshButton = document.querySelector<HTMLButtonElement>("#refresh")!;
const confirmButton = document.querySelector<HTMLButtonElement>("#confirm")!;
const form = document.querySelector<HTMLFormElement>("#setup-form")!;

let targets: Target[] = [];
let selectionRequired = false;
let capabilities: Capabilities | null = null;
let selection: Selection = { model: null, reasoningEffort: null };
let generation = 0;
let hostReady = false;
let selectionConfirmed = false;

function setStatus(message: string): void {
  status.textContent = message;
}

function currentModel(): SetupModel | undefined {
  return capabilities?.models.find((entry) => entry.model === selection.model);
}

function updateControls(): void {
  const target = targets.find((entry) => entry.id === targetSelect.value);
  targetSelect.disabled = !hostReady || selectionConfirmed || targets.length === 0;
  modelSelect.disabled = !hostReady || selectionConfirmed || !capabilities || Boolean(capabilities.error)
    || !capabilities.target.available;
  reasoningSelect.disabled = modelSelect.disabled || !selection.model;
  refreshButton.disabled = !hostReady || selectionConfirmed;
  confirmButton.disabled = !hostReady || selectionConfirmed || !capabilities
    || !selectionIsValid(capabilities, selection);
  if (target && !target.available) confirmButton.disabled = true;
}

function renderTargets(selectedId?: string): void {
  targetSelect.replaceChildren(new Option("Choose a target", ""));
  for (const target of targets) {
    const detail = target.available ? "Connected" : "Unavailable";
    const option = new Option(`${target.name} — ${target.id} (${detail})`, target.id);
    targetSelect.add(option);
  }
  if (selectedId && targets.some((target) => target.id === selectedId)) {
    targetSelect.value = selectedId;
  } else if (!selectionRequired && targets.length === 1) {
    targetSelect.value = targets[0].id;
  }
  updateControls();
}

function renderModels(): void {
  modelSelect.replaceChildren(new Option("Choose a model", ""));
  for (const model of capabilities?.models ?? []) {
    modelSelect.add(new Option(model.display_name, model.model));
  }
  modelSelect.value = selection.model ?? "";
  modelDescription.textContent = currentModel()?.description ?? "";

  reasoningSelect.replaceChildren(new Option("Choose an effort", ""));
  for (const effort of currentModel()?.reasoning_efforts ?? []) {
    reasoningSelect.add(new Option(effort.id, effort.id));
  }
  reasoningSelect.value = selection.reasoningEffort ?? "";
  const currentEffort = currentModel()?.reasoning_efforts.find(
    (effort) => effort.id === selection.reasoningEffort,
  );
  reasoningDescription.textContent = currentEffort?.description ?? "";
  updateControls();
}

function applySetupResult(result: SetupResult): void {
  selectionConfirmed = false;
  const previousId = targetSelect.value;
  targets = Array.isArray(result.targets) ? result.targets : [];
  selectionRequired = result.selection_required;
  const keptTarget = targets.find((target) => target.id === previousId);
  const selectedId = keptTarget?.id ?? (!selectionRequired && targets.length === 1 ? targets[0].id : "");
  renderTargets(selectedId);
  capabilities = null;
  selection = { model: null, reasoningEffort: null };

  const initialCapabilities = result.capabilities;
  if (initialCapabilities && initialCapabilities.target.id === selectedId) {
    capabilities = initialCapabilities;
    selection = applyCapabilities(initialCapabilities);
    showCapabilityStatus(initialCapabilities);
  } else if (selectedId) {
    void loadCapabilities(selectedId);
  } else {
    setStatus(targets.length ? "Select an execution target." : "No execution targets are configured.");
  }
  renderModels();
}

function showCapabilityStatus(value: Capabilities): void {
  if (!value.target.available) setStatus("Unavailable");
  else if (value.error) setStatus("Model capabilities are unavailable. Refresh to try again.");
  else setStatus("Connected");
}

function readStructured<T>(result: { structuredContent?: unknown; isError?: boolean }): T {
  if (result.isError || !result.structuredContent || typeof result.structuredContent !== "object") {
    throw new Error("The setup request failed.");
  }
  return result.structuredContent as T;
}

async function callSetup(): Promise<SetupResult> {
  const result = await app.callServerTool({ name: "codex_setup", arguments: {} });
  return readStructured<SetupResult>(result);
}

async function loadCapabilities(
  targetId: string,
  refreshTargets = false,
  previousSelection: Selection = selection,
): Promise<void> {
  const requestGeneration = ++generation;
  capabilities = null;
  selection = { model: null, reasoningEffort: null };
  renderModels();
  setStatus("Loading models…");
  try {
    if (refreshTargets) {
      const fresh = await callSetup();
      if (!isCurrentGeneration(requestGeneration, generation)) return;
      targets = fresh.targets;
      selectionRequired = fresh.selection_required;
      renderTargets(targetId);
      const target = targets.find((entry) => entry.id === targetId);
      if (!target || !target.available) {
        setStatus("Unavailable");
        updateControls();
        return;
      }
    }
    const result = await app.callServerTool({
      name: "codex_setup_capabilities",
      arguments: { target_id: targetId },
    });
    if (!isCurrentGeneration(requestGeneration, generation)) return;
    capabilities = readStructured<Capabilities>(result);
    selection = applyCapabilities(capabilities, previousSelection);
    renderModels();
    showCapabilityStatus(capabilities);
  } catch {
    if (!isCurrentGeneration(requestGeneration, generation)) return;
    capabilities = null;
    selection = { model: null, reasoningEffort: null };
    renderModels();
    setStatus("Model capabilities are unavailable. Refresh to try again.");
  }
}

targetSelect.addEventListener("change", () => {
  selectionConfirmed = false;
  const targetId = targetSelect.value;
  const previousSelection = selection;
  capabilities = null;
  selection = changeTarget();
  generation += 1;
  renderModels();
  const target = targets.find((entry) => entry.id === targetId);
  if (!targetId || !target) {
    setStatus("Select an execution target.");
  } else if (!target.available) {
    setStatus("Unavailable");
  } else {
    void loadCapabilities(targetId, false, previousSelection);
  }
});

modelSelect.addEventListener("change", () => {
  if (!capabilities) return;
  selectionConfirmed = false;
  selection = selectModel(capabilities, modelSelect.value, selection.reasoningEffort);
  renderModels();
});

reasoningSelect.addEventListener("change", () => {
  selectionConfirmed = false;
  selection = { ...selection, reasoningEffort: reasoningSelect.value || null };
  renderModels();
});

refreshButton.addEventListener("click", () => {
  const targetId = targetSelect.value;
  if (!targetId) {
    const requestGeneration = ++generation;
    void (async () => {
      try {
        const result = await callSetup();
        if (!isCurrentGeneration(requestGeneration, generation)) return;
        applySetupResult(result);
      } catch {
        if (!isCurrentGeneration(requestGeneration, generation)) return;
        setStatus("Could not refresh execution targets.");
      }
    })();
    return;
  }
  void loadCapabilities(targetId, true);
});

form.addEventListener("submit", (event) => {
  event.preventDefault();
  void confirmSelection();
});

async function confirmSelection(): Promise<void> {
  if (!capabilities || !selectionIsValid(capabilities, selection)) return;
  const targetId = capabilities.target.id;
  const model = selection.model;
  const reasoningEffort = selection.reasoningEffort;
  const requestGeneration = ++generation;
  confirmButton.disabled = true;
  setStatus("Confirming…");
  try {
    const result = await app.callServerTool({
      name: "codex_setup_confirm",
      arguments: confirmPayload(targetId, { model, reasoningEffort }),
    });
    if (!isCurrentGeneration(requestGeneration, generation)) return;
    const confirmed = readStructured<ConfirmResult>(result);
    if (confirmed.confirmed !== true) throw new Error("confirmation failed");

    const structured = confirmedContext(confirmed.selection);
    const text = [
      "CodexBridge setup confirmed:",
      `target_id=${confirmed.selection.target_id}`,
      `model=${confirmed.selection.model}`,
      `reasoning_effort=${confirmed.selection.reasoning_effort}`,
    ].join("\n");
    if (!app.getHostCapabilities()?.updateModelContext) {
      setStatus("This client cannot save the confirmed selection to the conversation.");
      return;
    }
    try {
      await app.updateModelContext({
        structuredContent: structured,
        content: [{ type: "text", text }],
      });
    } catch {
      setStatus("Selection confirmed, but this client could not save it to the conversation.");
      updateControls();
      return;
    }
    selectionConfirmed = true;
    updateControls();
    if (app.getHostCapabilities()?.message) {
      try {
        const sent = await app.sendMessage({
          role: "user",
          content: [
            {
              type: "text",
              text: "CodexBridge setup confirmed. Continue the pending task using the confirmed target, model, and reasoning effort.",
            },
          ],
        });
        if (!sent.isError) {
          setStatus("Selection confirmed");
          return;
        }
      } catch {
        // Context is already saved; let the user continue manually.
      }
    }
    setStatus("Selection confirmed. Continue in chat.");
  } catch {
    if (!isCurrentGeneration(requestGeneration, generation)) return;
    capabilities = null;
    selection = changeTarget();
    renderModels();
    setStatus("Refresh required. The selection may be stale or unavailable.");
  }
}

let initialToolResultHandled = false;
app.ontoolresult = (result) => {
  if (initialToolResultHandled) return;
  try {
    const setup = readStructured<SetupResult>(result);
    if (Array.isArray(setup.targets)) {
      initialToolResultHandled = true;
      applySetupResult(setup);
    }
  } catch {
    setStatus("Could not load setup data. Refresh to try again.");
  }
};

function applyHostTheme(): void {
  const theme = app.getHostContext()?.theme;
  document.documentElement.dataset.theme = theme === "dark" || theme === "light" ? theme : "auto";
}

app.addEventListener("hostcontextchanged", applyHostTheme);

async function start(): Promise<void> {
  try {
    await app.connect(new PostMessageTransport(window.parent, window.parent));
    applyHostTheme();
    const host = app.getHostCapabilities();
    if (!host?.serverTools) {
      setStatus("This client cannot load CodexBridge setup data. Use codex_setup text output in chat.");
      updateControls();
      return;
    }
    if (!host.updateModelContext) {
      setStatus("This client cannot save the confirmed selection to the conversation.");
      updateControls();
      return;
    }
    hostReady = true;
    refreshButton.disabled = false;
    setStatus("Loading targets…");
    // The host supplies the codex_setup result through ontoolresult.
  } catch {
    setStatus("This client does not provide the required MCP Apps setup capabilities.");
    hostReady = false;
    updateControls();
  }
}

void start();
