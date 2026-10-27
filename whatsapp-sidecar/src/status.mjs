// Disconnect-code -> action mapping (pure, unit tested). Codes follow Baileys' DisconnectReason.
export const CODE = { loggedOut: 401, forbidden: 403, timedOut: 408, connectionReplaced: 440, restartRequired: 515 }

export const isRegistered = (creds) => !!(creds?.registered || creds?.me?.id)

// leaseHeld: result of lease.verify() taken AFTER the close. A 401 only counts as a real logout while we provably hold the lease.
export function classifyDisconnect({ code, registered, leaseHeld, consecutiveRestarts = 0, consecutiveConflicts = 0 }) {
  if (code === CODE.restartRequired && consecutiveRestarts < 5) return { action: 'restart', status: 'connecting' }
  if (code === CODE.loggedOut) {
    return leaseHeld ? { action: 'logout', status: 'logged_out' } : { action: 'lease_lost', status: 'reconnecting' }
  }
  if (code === CODE.forbidden) return { action: 'stop', status: 'failed' }                 // restricted/banned: never hammer it
  if (!registered) return { action: 'idle', status: 'awaiting_pair' }                       // pairing socket ended; never auto-retry pairing
  if (code === CODE.connectionReplaced && consecutiveConflicts >= 3) return { action: 'stop', status: 'failed' }   // something outside the lease uses these creds
  return { action: 'reconnect', status: 'reconnecting' }
}

export const backoffMs = (attempt, random = Math.random) => Math.min(60000, 2000 * 2 ** Math.min(attempt, 6)) * (0.75 + random() * 0.5)
