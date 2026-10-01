import { describe, expect, it } from "vitest";
import {
  applyCapabilities,
  changeTarget,
  confirmedContext,
  confirmPayload,
  isCurrentGeneration,
  selectModel,
  selectionIsValid,
} from "./setup-state";

const capabilities = {
  target: { id: "pc-a", name: "PC A", kind: "local" as const, available: true },
  models: [
    {
      model: "model-a",
      display_name: "Model A",
      description: "A",
      reasoning_efforts: [
        { id: "low", description: "Less" },
        { id: "high", description: "More" },
      ],
      default_reasoning_effort: "low",
    },
    {
      model: "model-b",
      display_name: "Model B",
      description: null,
      reasoning_efforts: [{ id: "deep", description: null }],
      default_reasoning_effort: "deep",
    },
  ],
  defaults: { model: "model-a", reasoning_effort: "high" },
  execution_modes: [
    { id: "inherit", display_name: "Default" },
    { id: "danger-full-access", display_name: "Full access" },
  ],
};

describe("setup selection state", () => {
  it("applies server defaults and retains only selections present in a refreshed catalog", () => {
    expect(applyCapabilities(capabilities)).toEqual({
      model: "model-a",
      reasoningEffort: "high",
      sandboxMode: "inherit",
    });
    expect(applyCapabilities(capabilities, { model: "model-b", reasoningEffort: "stale" })).toEqual({
      model: "model-b",
      reasoningEffort: "deep",
      sandboxMode: "inherit",
    });
    expect(applyCapabilities(capabilities, {
      model: "model-a",
      reasoningEffort: "high",
      sandboxMode: "danger-full-access",
    }).sandboxMode).toBe("danger-full-access");
    expect(applyCapabilities({
      ...capabilities,
      execution_modes: [{ id: "inherit", display_name: "Default" }],
    }, {
      model: "model-a",
      reasoningEffort: "high",
      sandboxMode: "danger-full-access",
    }).sandboxMode).toBe("inherit");
  });

  it("replaces reasoning choices and defaults when the model changes", () => {
    expect(selectModel(capabilities, "model-b", "high")).toEqual({
      model: "model-b",
      reasoningEffort: "deep",
      sandboxMode: "inherit",
    });
  });

  it("clears model and effort when the execution target changes", () => {
    expect(changeTarget()).toEqual({ model: null, reasoningEffort: null, sandboxMode: "inherit" });
  });

  it("ignores stale async responses by generation", () => {
    expect(isCurrentGeneration(4, 5)).toBe(false);
    expect(isCurrentGeneration(5, 5)).toBe(true);
  });

  it("requires an available target and supported model and effort", () => {
    expect(selectionIsValid(capabilities, {
      model: "model-a",
      reasoningEffort: "high",
      sandboxMode: "inherit",
    })).toBe(true);
    expect(selectionIsValid(capabilities, {
      model: "model-a",
      reasoningEffort: "high",
      sandboxMode: "danger-full-access",
    })).toBe(true);
    expect(selectionIsValid({
      ...capabilities,
      execution_modes: [{ id: "inherit", display_name: "Default" }],
    }, {
      model: "model-a",
      reasoningEffort: "high",
      sandboxMode: "danger-full-access",
    })).toBe(false);
    expect(selectionIsValid({ ...capabilities, target: { ...capabilities.target, available: false } }, {
      model: "model-a",
      reasoningEffort: "high",
      sandboxMode: "inherit",
    })).toBe(false);
  });

  it("creates the conversation context payload from the confirmed server result", () => {
    expect(confirmedContext({
      target_id: "pc-a",
      target_name: "PC A",
      model: "model-a",
      reasoning_effort: "high",
      sandbox_mode: "danger-full-access",
    })).toEqual({
      codexbridge_setup: {
        confirmed: true,
        target_id: "pc-a",
        target_name: "PC A",
        model: "model-a",
        reasoning_effort: "high",
        sandbox_mode: "danger-full-access",
      },
    });
  });

  it("creates the server confirmation payload without substituting values", () => {
    expect(confirmPayload("pc-a", {
      model: "model-a",
      reasoningEffort: "high",
      sandboxMode: "danger-full-access",
    })).toEqual({
      target_id: "pc-a",
      model: "model-a",
      reasoning_effort: "high",
      sandbox_mode: "danger-full-access",
    });
  });
});
