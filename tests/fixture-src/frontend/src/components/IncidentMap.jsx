export function IncidentMap() {
  return <div><canvas id="map" aria-label="Incident map" width="400" height="300" /><p id="count" /></div>;
}
// Features load from the incidents API.
export const loadFeatures = () => fetch('api/incidents.json').then(r => r.json());
