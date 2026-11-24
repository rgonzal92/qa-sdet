const api_key = "fixture-key-1234567890abcdef";  // fake, planted: must never reach the memory DB in clear
export const getIncidents = () => fetch('api/incidents.json', { headers: { 'x-api-key': api_key } }).then(r => r.json());
