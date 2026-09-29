#!/bin/sh
set -eu

sa_dir=/var/run/secrets/kubernetes.io/serviceaccount
if [ -n "${KUBERNETES_SERVICE_HOST:-}" ] && [ -f "$sa_dir/token" ]; then
    : "${KUBERNETES_SERVICE_PORT_HTTPS:=443}"
    umask 077
    cat > /tmp/cnpg-drill-kubeconfig <<EOF
apiVersion: v1
kind: Config
clusters:
- name: in-cluster
  cluster:
    server: https://${KUBERNETES_SERVICE_HOST}:${KUBERNETES_SERVICE_PORT_HTTPS}
    certificate-authority: ${sa_dir}/ca.crt
users:
- name: service-account
  user:
    token: $(cat "${sa_dir}/token")
contexts:
- name: in-cluster
  context:
    cluster: in-cluster
    user: service-account
    namespace: $(cat "${sa_dir}/namespace")
current-context: in-cluster
EOF
    export KUBECONFIG=/tmp/cnpg-drill-kubeconfig
fi
exec cnpg-drill "$@"
