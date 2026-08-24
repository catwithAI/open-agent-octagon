/** 模型/场景的 modality 徽标（OpenRouter MODALITIES 风格）。
 *
 * OpenRouter 的展示：输入 modality 图标一组 → 输出 modality 图标一组，
 * text=蓝 T、image=绿图、audio=粉耳机、video=红摄像机。这里用同色系
 * chip 复刻；`ModalityBadges` 渲染 "输入 → 输出" 完整行，
 * `ModalityChip` 渲染单个。
 */

import { useI18n } from "../i18n";

const MODALITY_META: Record<string, { icon: string; cls: string }> = {
  text: { icon: "T", cls: "mod-text" },
  image: { icon: "🖼", cls: "mod-image" },
  audio: { icon: "🎧", cls: "mod-audio" },
  video: { icon: "🎬", cls: "mod-video" },
  file: { icon: "📄", cls: "mod-file" },
};

// modality → 译名 key（未知 modality 直接回落到原 id）。
const MODALITY_LABEL_KEY: Record<string, string> = {
  text: "modalityChips.label.text",
  image: "modalityChips.label.image",
  audio: "modalityChips.label.audio",
  video: "modalityChips.label.video",
  file: "modalityChips.label.file",
};

export function ModalityChip({ modality }: { modality: string }) {
  const { t } = useI18n();
  const meta = MODALITY_META[modality] ?? { icon: modality[0]?.toUpperCase() ?? "?", cls: "mod-other" };
  const labelKey = MODALITY_LABEL_KEY[modality];
  const label = labelKey ? t(labelKey) : modality;
  return (
    <span className={`mod-chip ${meta.cls}`} title={t("modalityChips.title", { label, modality })}>
      {meta.icon}
    </span>
  );
}

export function ModalityBadges({
  input, output,
}: {
  input?: string[] | null;
  output?: string[] | null;
}) {
  const ins = input ?? [];
  const outs = output ?? [];
  if (ins.length === 0 && outs.length === 0) return null;
  return (
    <span className="mod-badges" aria-label="modalities">
      {ins.map((m) => <ModalityChip key={`in-${m}`} modality={m} />)}
      {outs.length > 0 && <span className="mod-arrow">→</span>}
      {outs.map((m) => <ModalityChip key={`out-${m}`} modality={m} />)}
    </span>
  );
}

/** env 声明的 agent 侧 modality 需求 与 所选模型 input_modalities 的交叉
 * 检查。返回缺失的 modality 列表（空=兼容或无从判断）。 */
export function missingModalities(
  required?: string[] | null,
  modelInput?: string[] | null,
): string[] {
  if (!required || required.length === 0) return [];
  // 模型 modality 数据缺失（如手输的非 OpenRouter 模型名）时无从判断，不误报
  if (!modelInput || modelInput.length === 0) return [];
  return required.filter((m) => !modelInput.includes(m));
}


/** 原生 <option> 里放不了组件——env 下拉的 modality 需求用 emoji 文案标记
 * （text 是所有模型都有的基线，不标）。 */
export function modalityOptionMark(modalities?: string[] | null): string {
  if (!modalities || modalities.length === 0) return "";
  const marks = modalities
    .filter((m) => m !== "text")
    .map((m) => MODALITY_META[m]?.icon ?? m)
    .join("");
  return marks ? ` ${marks}` : "";
}
