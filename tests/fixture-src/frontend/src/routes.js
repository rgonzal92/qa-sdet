// Router for the fixture app (a static site, so routes are file names).
export const routes = [
  { path: '/index.html', name: 'Incidents', component: 'IncidentsTable' },
  { path: '/form.html', name: 'Speed zone', component: 'SpeedZoneForm' },
  { path: '/map.html', name: 'Map', component: 'IncidentMap' },
];
