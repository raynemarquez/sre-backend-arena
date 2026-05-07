/**
 * Wizard Intelligence Network — Load Test
 * Ferramenta: k6 (https://k6.io)
 *
 * Instalação no Windows:
 *   winget install k6 --source winget
 *   ou baixe em: https://github.com/grafana/k6/releases
 *
 * Execução (porta-forward da app deve estar ativa):
 *   kubectl port-forward svc/wizard-intelligence-network 8000:8000 -n wizard
 *
 * Rampas disponíveis:
 *   Smoke test (sanidade):   k6 run --env SCENARIO=smoke load_test.js
 *   Stress test (10k RPS):   k6 run --env SCENARIO=stress load_test.js
 *   Soak test (estabilidade):k6 run --env SCENARIO=soak load_test.js
 *   Spike test (burst):      k6 run --env SCENARIO=spike load_test.js
 *   Padrão (stress):         k6 run load_test.js
 */

import http from 'k6/http';
import { check, sleep } from 'k6';
import { Counter, Rate, Trend } from 'k6/metrics';

// ---------------------------------------------------------------------------
// Métricas customizadas
// ---------------------------------------------------------------------------
const cacheHits    = new Counter('wizard_cache_hits');
const cacheMisses  = new Counter('wizard_cache_misses');
const errorRate    = new Rate('wizard_error_rate');
const wizardTrend  = new Trend('wizard_response_time', true);

// ---------------------------------------------------------------------------
// Wizards para variar as requisições (mix de cache hit e miss)
// ---------------------------------------------------------------------------
const WIZARDS = [
  'Harry Potter',
  'Hermione Granger',
  'Ron Weasley',
  'Albus Dumbledore',
  'Severus Snape',
  'Draco Malfoy',
  'Neville Longbottom',
  'Luna Lovegood',
  'Sirius Black',
  'Remus Lupin',
];

// ---------------------------------------------------------------------------
// Cenários de carga
// ---------------------------------------------------------------------------
const SCENARIOS = {
  // Sanidade — 1 VU, 30s — verifica se a app responde corretamente
  smoke: {
    executor: 'constant-vus',
    vus: 1,
    duration: '30s',
  },

  // Stress — rampa até carga alta para medir throughput máximo
  stress: {
    executor: 'ramping-arrival-rate',
    startRate: 10,
    timeUnit: '1s',
    preAllocatedVUs: 500, // Começa com pool alto para evitar cold start do k6
    maxVUs: 2000,         // Aumentado para garantir 10k RPS se a latência subir
    stages: [
      { duration: '30s', target: 50   },  // aquecimento
      { duration: '1m',  target: 200  },  // sobe para 200 RPS
      { duration: '2m',  target: 500  },  // sobe para 500 RPS
      { duration: '2m',  target: 10000 }, // sobe para 10000 RPS
      { duration: '1m',  target: 500  },  // desce
      { duration: '30s', target: 0    },  // cooldown
    ],
  },

  // Soak — carga moderada por tempo longo (detecta memory leaks)
  soak: {
    executor: 'constant-arrival-rate',
    rate: 100,
    timeUnit: '1s',
    duration: '10m',
    preAllocatedVUs: 50,
    maxVUs: 100,
  },

  // Spike — burst repentino (testa circuit breaker e cache sob pressão)
  spike: {
    executor: 'ramping-arrival-rate',
    startRate: 10,
    timeUnit: '1s',
    preAllocatedVUs: 300,
    maxVUs: 600,
    stages: [
      { duration: '10s', target: 10   },  // normal
      { duration: '5s',  target: 500  },  // spike abrupto
      { duration: '1m',  target: 500  },  // mantém spike
      { duration: '5s',  target: 10   },  // cai abruptamente
      { duration: '30s', target: 10   },  // recuperação
    ],
  },
};

// ---------------------------------------------------------------------------
// Thresholds — critérios de aprovação/falha do teste
// ---------------------------------------------------------------------------
export const options = {
  scenarios: {
    wizard_load: SCENARIOS[__ENV.SCENARIO || 'stress'],
  },
  thresholds: {
    // SLO de latência: p99 < 300ms, p95 < 150ms
    'wizard_response_time': ['p(99)<300', 'p(95)<150', 'p(50)<100'], // Bónus de performance
    // Taxa de erro < 1%
    'wizard_error_rate': ['rate<0.01'], // Zero 5xx (quase)
    // HTTP failures < 1%
    'http_req_failed': ['rate<0.01'],
    // Latência geral p99 < 300ms
    'http_req_duration': ['p(99)<300'],
  },
};

// ---------------------------------------------------------------------------
// Função principal
// ---------------------------------------------------------------------------
export default function () {
//  const BASE_URL = __ENV.BASE_URL || 'http://localhost:8000';
// URL configurada para o Ingress do k3d (sem port-forward)
  const BASE_URL = __ENV.BASE_URL || 'http://wizard-arena.local:8080';

  // Escolhe um wizard aleatório — gera mix de cache hits e misses
  const wizard = WIZARDS[Math.floor(Math.random() * WIZARDS.length)];
  const url    = `${BASE_URL}/wizard/${encodeURIComponent(wizard)}`;

  const params = {
    headers: {
      'X-Request-ID': `k6-${__VU}-${__ITER}`,
      'Accept': 'application/json',
    },
    timeout: '5s',
  };

  const res = http.get(url, params);
  const ok  = res.status === 200;
  const notFound = res.status === 404;  // wizard não encontrado é válido
  const circuitOpen = res.status === 503;

  // Registo de Métricas baseadas no Header X-Cache (Backend injeta este header)
  if (ok && res.headers['X-Cache'] === 'HIT') {
    cacheHits.add(1);
  } else if (ok) {
    cacheMisses.add(1);
  }

  // Métricas de tendência
  wizardTrend.add(res.timings.duration);
  errorRate.add(res.status >= 500 && res.status !== 503);

  // Validações 
  check(res, {
    'status 200 ou 404':     (r) => r.status === 200 || r.status === 404,
    'X-Cache presente':      (r) => r.headers['X-Cache'] !== undefined,
    'Corpo da resposta OK':  (r) => {
									  
      if (r.status !== 200) return true;
		   
      const b = JSON.parse(r.body);
      return b.name && b.house !== undefined && b.powerScore !== undefined;
							   
    },
  });

  if (circuitOpen) {
    // Circuit breaker aberto — espera um pouco antes de continuar
    sleep(0.1);
  }
}

// ---------------------------------------------------------------------------
// Sumário customizado no final
// ---------------------------------------------------------------------------
export function handleSummary(data) {
  const dur    = data.metrics['wizard_response_time'];
  const errors = data.metrics['wizard_error_rate'];
  const rps    = data.metrics['http_reqs'];

  const fmt = (v) => (v !== undefined && v !== null) ? v.toFixed(2) : 'N/A';

  console.log('\n========================================');
  console.log('  WIZARD INTELLIGENCE NETWORK — RESULTADO');
  console.log('========================================');
  if (dur && dur.values) {
    console.log(`  p50 latência:  ${fmt(dur.values['p(50)'])}ms`);
    console.log(`  p95 latência:  ${fmt(dur.values['p(95)'])}ms`);
    console.log(`  p99 latência:  ${fmt(dur.values['p(99)'])}ms`);
    console.log(`  avg latência:  ${fmt(dur.values['avg'])}ms`);
  }
  if (rps && rps.values)    console.log(`  RPS médio:     ${fmt(rps.values['rate'])}`);
  if (errors && errors.values) console.log(`  Taxa de erro:  ${((errors.values['rate'] || 0) * 100).toFixed(2)}%`);
  console.log('========================================\n');

  return {};
}