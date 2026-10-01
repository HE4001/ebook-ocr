import { useId } from 'react'
import type { ApiProtocol } from './types'
import './reasoning-control.css'

type ReasoningControlProps = {
  protocol: ApiProtocol
  value: string
  disabled: boolean
  onChange: (value: string) => void
}

export function ReasoningControl({ protocol, value, disabled, onChange }: ReasoningControlProps) {
  const id = useId()
  const isGemini = protocol === 'gemini'

  return (
    <fieldset className="reasoning-control" disabled={disabled} aria-describedby={`${id}-hint`}>
      <legend className="reasoning-legend">模型推理程度</legend>
      <p className="reasoning-hint" id={`${id}-hint`}>留空使用服务默认值，具体支持的选项以所选模型为准。</p>
      <label className="reasoning-custom" htmlFor={`${id}-value`}>
        <span className="reasoning-custom-label">手动输入</span>
        <input
          className="reasoning-input"
          id={`${id}-value`}
          value={value}
          onChange={(event) => onChange(event.target.value)}
          placeholder={isGemini ? '级别或整数预算' : '供应商支持的值'}
          aria-describedby={`${id}-hint`}
          autoComplete="off"
          spellCheck={false}
        />
      </label>
      {isGemini && (
        <details className="reasoning-details">
          <summary className="reasoning-summary">Gemini 级别与预算说明</summary>
          <div className="reasoning-detail-content">
            <p className="reasoning-detail-line"><strong>Gemini 3</strong>：级别通过 <code>thinkingLevel</code> 传递，包括 minimal、low、medium、high；可用级别取决于模型。</p>
            <p className="reasoning-detail-line"><strong>Gemini 2.5</strong>：在输入框中填写不小于 <code>-1</code> 的整数预算，通过 <code>thinkingBudget</code> 传递。<code>-1</code> 表示动态预算，<code>0</code> 表示关闭；是否可用及预算范围取决于模型。</p>
          </div>
        </details>
      )}
    </fieldset>
  )
}
