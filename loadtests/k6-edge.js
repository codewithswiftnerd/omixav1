// k6 alternative for the cheap, high-fan-out scenarios (landing, health, job-status polling).
// k6 run -e BASE=https://staging.example.com -e STAGE=10000 loadtests/k6-edge.js
import http from 'k6/http';
import { check, sleep } from 'k6';

const target = Number(__ENV.STAGE || 1000);
export const options = {
  scenarios: {
    ramp: { executor: 'ramping-vus', stages: [
      { duration: '2m', target: target }, { duration: '10m', target: target }, { duration: '1m', target: 0 } ] },
  },
  thresholds: { http_req_failed: ['rate<0.01'], http_req_duration: ['p(95)<800', 'p(99)<2000'] },
};

export default function () {
  const r = http.get(`${__ENV.BASE}/`);
  check(r, { 'home 200': (x) => x.status === 200 });
  http.get(`${__ENV.BASE}/healthz`);
  sleep(1 + Math.random() * 4);
}
