import { getIncidents } from '../api/client';

export function IncidentsTable() {
  const rows = getIncidents();
  return (
    <table id="t" aria-label="Incidents">
      <thead><tr>{['id', 'road', 'type'].map(k => <th key={k}><button data-k={k}>{k}</button></th>)}</tr></thead>
      <tbody>{rows.map(r => <tr key={r.id}><td>{r.id}</td><td>{r.road}</td><td>{r.type}</td></tr>)}</tbody>
    </table>
  );
}
