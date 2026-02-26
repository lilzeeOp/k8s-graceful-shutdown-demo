import http from "k6/http";
import { check, sleep } from "k6";
import { Counter, Rate, Trend } from "k6/metrics";

// Custom metrics
const errorRate = new Rate("error_rate");
const errorCount = new Counter("error_count");
const successCount = new Counter("success_count");
const latency = new Trend("request_latency", true);

export const options = {
  vus: 15,
  duration: "60s",
  thresholds: {
    error_rate: [{ threshold: "rate<0.01", abortOnFail: false }],
  },
};

export default function () {
  const res = http.get("http://localhost:8000/");

  const isSuccess = res.status === 200;

  check(res, {
    "status is 200": (r) => r.status === 200,
    "response has upstream data": (r) => {
      try {
        const body = JSON.parse(r.body);
        return body.status === "ok";
      } catch {
        return false;
      }
    },
  });

  if (isSuccess) {
    successCount.add(1);
    errorRate.add(false);
  } else {
    errorCount.add(1);
    errorRate.add(true);
    console.log(
      `ERROR: status=${res.status} body=${res.body ? res.body.substring(0, 200) : "empty"}`
    );
  }

  latency.add(res.timings.duration);

  // Small pause between requests
  sleep(0.1);
}

export function handleSummary(data) {
  const totalRequests =
    (data.metrics.success_count?.values?.count || 0) +
    (data.metrics.error_count?.values?.count || 0);
  const errors = data.metrics.error_count?.values?.count || 0;
  const rate = data.metrics.error_rate?.values?.rate || 0;

  console.log("\n========== SUMMARY ==========");
  console.log(`Total requests: ${totalRequests}`);
  console.log(`Errors:         ${errors}`);
  console.log(`Error rate:     ${(rate * 100).toFixed(2)}%`);
  console.log("=============================\n");

  return {
    stdout: textSummary(data, { indent: " ", enableColors: true }),
  };
}

// k6 built-in text summary
function textSummary(data, opts) {
  // k6 handles this internally when we return from handleSummary
  return JSON.stringify(data, null, 2);
}
