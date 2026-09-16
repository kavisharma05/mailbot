export function todayIST() {
  return new Intl.DateTimeFormat('en-CA', { timeZone: 'Asia/Kolkata' }).format(new Date())
}

export function nowIST() {
  return new Date(new Date().toLocaleString('en-US', { timeZone: 'Asia/Kolkata' }))
}

export function shortDeadline(display) {
  if (!display) return ''
  const str = String(display)
  
  // Remove parenthetical content first
  const cleaned = str.split(' (')[0].trim()
  
  // If it starts with "Due in X" extract just the time part
  const dueInMatch = cleaned.match(/^Due in (\d+[hmd]\s*\d*[hmd]?)/i)
  if (dueInMatch) {
    return dueInMatch[1].trim()
  }
  
  // If it has " — " separator, take the first part (the relative time)
  if (cleaned.includes(' — ')) {
    const firstPart = cleaned.split(' — ')[0].trim()
    // If the first part starts with "Due in", extract just the time
    const innerMatch = firstPart.match(/^Due in (.+)$/i)
    if (innerMatch) {
      return innerMatch[1].trim()
    }
    return firstPart
  }
  
  // If it's a full date like "Wed, 16 Sep 2026 11:59 PM", shorten it
  const dateMatch = cleaned.match(/^(\w{3}),?\s*(\d{1,2})\s+(\w{3})/)
  if (dateMatch) {
    return `${dateMatch[1]}, ${dateMatch[2]} ${dateMatch[3]}`
  }
  
  // Return cleaned version, but truncate if too long
  return cleaned.length > 20 ? cleaned.slice(0, 18) + '…' : cleaned
}
