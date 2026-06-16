import * as React from "react";
import type { ModuleId } from "../nav";
import { findItem } from "../nav";
import { Overview } from "./Overview";
import { Matches } from "./Matches";
import { Rulesets } from "./Rulesets";
import { Agents } from "./Agents";
import { Speech } from "./Speech";
import { Xiaoqi } from "./Xiaoqi";
import { Data } from "./Data";
import { Logs } from "./Logs";
import { Security } from "./Security";
import { Debaters } from "./Debaters";
import { Flow } from "./Flow";
import { Diagnostics } from "./Diagnostics";
import { Control } from "./Control";
import { DebateProcess } from "./DebateProcess";
import { Placeholder } from "./Placeholder";

function ph(id: ModuleId) {
  return function PlaceholderModule() {
    return <Placeholder label={findItem(id)?.label ?? id} />;
  };
}

export const MODULES: Record<ModuleId, React.ComponentType> = {
  overview: Overview,
  matches: Matches,
  rulesets: Rulesets,
  agents: Agents,
  speech: Speech,
  xiaoqi: Xiaoqi,
  data: Data,
  logs: Logs,
  security: Security,
  debaters: Debaters,
  flow: Flow,
  diagnostics: Diagnostics,
  control: Control,
  "debate-process": DebateProcess,
};
