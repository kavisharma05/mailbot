function stripWhen(text) {
  if (!text) return ''
  return String(text)
    .replace(/\s*[—–-]\s*due\b.*/i, '')
    .replace(/\s+by\s+\d{1,2}\s+\w+(\s+\d{4})?.*$/i, '')
    .trim()
}

function isGenericTitle(title) {
  const t = title.toLowerCase().trim()
  return /^(complete|submit|finish|do|fill)?\s*(the\s+)?(registration|form|task)$/i.test(t)
}

export function displayTitle(task) {
  if (!task) return ''
  const raw = (task.task || '').trim()
  const course = (task.course_or_topic || '').trim()
  const summary = stripWhen(task.summary)

  if (isGenericTitle(raw) && course) {
    if (/registration/i.test(raw)) return `Complete ${course} registration`
    if (/form/i.test(raw)) return `Complete ${course} form`
    return `${raw} — ${course}`
  }
  if (isGenericTitle(raw) && summary && summary.length > raw.length + 3) {
    return summary
  }
  return raw
}

function largelyCovered(candidate, title) {
  const words = candidate.toLowerCase().split(/\s+/).filter((w) => w.length > 2)
  if (!words.length) return true
  const hit = words.filter((w) => title.includes(w)).length
  return hit / words.length >= 0.7
}

export function displayDetail(task) {
  if (!task) return ''
  const title = displayTitle(task).toLowerCase()
  const course = (task.course_or_topic || '').trim()
  const detail = (task.detail || '').trim()
  const summary = stripWhen(task.summary)

  for (const candidate of [detail, summary, course, task.sender || '']) {
    const text = String(candidate || '').trim()
    if (!text) continue
    const lower = text.toLowerCase()
    if (lower === title) continue
    if (title.includes(lower) || largelyCovered(text, title)) continue
    if (lower.includes(title) && text.length > title.length + 8) return text
    return text
  }
  return ''
}
