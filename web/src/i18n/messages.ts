/**
 * i18n 文案字典：扁平 key → { zh, en }。
 *
 * 命名约定：`<区域>.<用途>`，如 `shell.nav.overview`。新增文案在此登记，
 * 组件里用 t("shell.nav.overview") 取。zh 是原文，en 是英文。
 */
export type Message = { zh: string; en: string };

import { MESSAGES_RUN_DETAIL } from "./messages.runDetail";
import { MESSAGES_G2 } from "./messages.g2";
import { MESSAGES_G3 } from "./messages.g3";
import { MESSAGES_G4 } from "./messages.g4";
import { MESSAGES_G5 } from "./messages.g5";

export const MESSAGES: Record<string, Message> = {
  ...MESSAGES_RUN_DETAIL,
  ...MESSAGES_G2,
  ...MESSAGES_G3,
  ...MESSAGES_G4,
  ...MESSAGES_G5,
  // ---- AppShell / 顶栏 / 导航 ----
  "shell.skipToContent": { zh: "跳到主要内容", en: "Skip to content" },
  "shell.closeNav": { zh: "关闭导航", en: "Close navigation" },
  "shell.openNav": { zh: "打开导航", en: "Open navigation" },
  "shell.mainNav": { zh: "主导航", en: "Main navigation" },
  "shell.brandAria": { zh: "OCTAGON 总览", en: "OCTAGON overview" },
  "shell.nav.overview": { zh: "总览", en: "Overview" },
  "shell.nav.newExperiment": { zh: "新建实验", en: "New experiment" },
  "shell.nav.experiments": { zh: "实验列表", en: "Experiments" },
  "shell.nav.resources": { zh: "资源库", en: "Resources" },
  "shell.nav.scenarios": { zh: "场景库", en: "Scenarios" },
  "shell.nav.profiles": { zh: "配置模板", en: "Profiles" },
  "shell.legacy.summary": { zh: "兼容评测入口", en: "Legacy entrypoints" },
  "shell.legacy.multiAgent": { zh: "多 Agent", en: "Multi-agent" },
  "shell.legacy.sameModel": { zh: "同模型", en: "Same model" },
  "shell.legacy.multiModel": { zh: "多模型", en: "Multi model" },
  "shell.legacy.runs": { zh: "运行记录", en: "Runs" },
  "shell.capability.negotiating": { zh: "正在协商能力", en: "Negotiating capabilities" },
  "shell.capability.available": { zh: "研究工作区可用", en: "Research workspace available" },
  "shell.capability.limited": { zh: "研究工作区受限", en: "Research workspace limited" },
  "shell.capability.version": { zh: "能力协商 v1", en: "Capability negotiation v1" },
  "shell.capability.enabled": { zh: "{n}/{total} 启用", en: "{n}/{total} enabled" },
  "shell.newExperiment": { zh: "新建实验", en: "New experiment" },
  "shell.lang.aria": { zh: "切换语言", en: "Switch language" },

  // ---- 面包屑（路由标签）----
  "crumb.overview": { zh: "总览", en: "Overview" },
  "crumb.experiments": { zh: "实验列表", en: "Experiments" },
  "crumb.experimentNew": { zh: "实验列表 / 新建实验", en: "Experiments / New" },
  "crumb.experimentDetail": { zh: "实验列表 / 实验详情", en: "Experiments / Detail" },
  "crumb.experimentLive": { zh: "实验列表 / 实验详情 / 实时", en: "Experiments / Detail / Live" },
  "crumb.experimentResults": { zh: "实验列表 / 实验详情 / 结果", en: "Experiments / Detail / Results" },
  "crumb.experimentInsights": { zh: "实验列表 / 实验详情 / 结果 / 洞察", en: "Experiments / Detail / Results / Insights" },
  "crumb.scenarios": { zh: "场景库", en: "Scenarios" },
  "crumb.profiles": { zh: "配置模板", en: "Profiles" },
  "crumb.system": { zh: "系统状态", en: "System status" },
  "crumb.runs": { zh: "运行记录", en: "Runs" },
  "crumb.runDetail": { zh: "运行记录 / 运行详情", en: "Runs / Detail" },
  "crumb.sameModel": { zh: "快速评测 / 同模型", en: "Quick eval / Same model" },
  "crumb.multiModel": { zh: "快速评测 / 多模型", en: "Quick eval / Multi model" },
  "crumb.multiAgent": { zh: "快速评测 / 多 Agent", en: "Quick eval / Multi-agent" },
};
