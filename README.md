# Proxmox → NetBox Sync Script

Script de synchronisation automatique et intelligent des machines virtuelles Proxmox vers NetBox, avec option de nettoyage des éléments obsolètes.

## Fonctionnalités principales

- Synchronisation : VMs, interfaces réseau, adresses IP, disques virtuels et plateformes OS.
- Nettoyage automatique : suppression des ressources NetBox qui n'existent plus dans Proxmox (optionnel).
- Détection Public/Private : classification automatique selon les plages IP.
- Support QEMU Guest Agent : récupération d'informations OS et réseau en temps réel, avec fallback.
- Mode Dry-Run : simulation des modifications sans appliquer.
- Logging détaillé et statistiques.

---

## Prérequis

### Versions recommandées
- NetBox : ≥ 3.5 (testé sur 4.0.x)
- Proxmox VE : ≥ 7.0 (API v2)
- Python : 3.8+ (3.11 recommandé)

### Dépendances Python
Ces dépendances sont généralement fournies par NetBox :
```text
requests>=2.28.0
urllib3>=1.26.0
django>=4.0
```

### Permissions NetBox
L'utilisateur NetBox utilisé par le script doit disposer des permissions appropriées (ex. `virtualization.add_virtualmachine`, `ipam.change_ipaddress`, etc.). Activez aussi `delete_*` si vous souhaitez le nettoyage automatique.

---

## Installation

1. Connectez-vous sur le serveur NetBox :
   ```bash
   ssh user@netbox-server
   sudo su - netbox
   source /opt/netbox/venv/bin/activate
   ```

2. Créez le répertoire des scripts s'il n'existe pas :
   ```bash
   cd /opt/netbox/netbox
   mkdir -p scripts
   ```

3. Téléchargez le script (exemple) :
   ```bash
   cd /opt/netbox/netbox/scripts
   wget https://raw.githubusercontent.com/Steph-ux/proxmox-netbox-sync/main/proxmox_sync.py
   chown netbox:netbox proxmox_sync.py
   chmod 644 proxmox_sync.py
   ```

4. Tester l'import dans nbshell :
   ```bash
   python3 manage.py nbshell
   >>> from scripts.proxmox_sync import ProxmoxSync
   >>> exit()
   ```

5. Redémarrer NetBox si nécessaire :
   ```bash
   sudo systemctl restart netbox netbox-rq
   ```

---

## Configuration du token API Proxmox

1. Dans l'interface Proxmox (Datacenter → Permissions → API Tokens → Add) :
   - User: `root@pam` (ou utilisateur dédié)
   - Token ID: `netbox-sync`
   - Notez : `root@pam!netbox-sync` et le secret (affiché une seule fois).

2. Permissions recommandées (si utilisation d'un rôle restreint) :
   ```bash
   pveum role add NetBoxSync -privs "VM.Audit,Sys.Audit,Datastore.Audit"
   pveum acl modify / -user root@pam -role NetBoxSync
   ```

---

## Utilisation (via l'interface NetBox)

1. Operations → Integrations → Data Sources → +Add
   - Type: `local`
   - URL: `/opt/netbox/netbox/scripts`

2. Customization → Scripts → +Add
   - Data Source: *le Data Source créé*
   - File: `proxmox_sync.py`

3. Lancer le script depuis l'interface (Run Script). Pour tests, utilisez le mode Dry-Run (ne pas cocher "Commit changes").

---

## Paramètres clés

- Cluster NetBox (où créer/mettre à jour les VMs)
- Serveur Proxmox (IP/hostname)
- Token ID et Token Secret
- Options : synchroniser interfaces, plateformes, disques, nettoyage automatique, définir IP primaire, etc.

Exemples d'usage :
- Synchronisation complète (toutes options activées) — ATTENTION au nettoyage.
- Synchronisation sans suppression (désactiver le nettoyage) pour validation.
- Mode minimal : VMs uniquement (désactiver interfaces/disques/platforms).

---

## Fonctionnement et logique importante

- Détection Private/Public basée sur RFC1918 (10/8, 172.16/12, 192.168/16) ; autres plages considérées publiques.
- Si QEMU Guest Agent disponible : noms d'interfaces réels, IP en temps réel, OS.
- Sans agent : fallback sur données Proxmox (noms génériques net0, net1,...).

---

## Logs et diagnostics

- Logs détaillés visibles après exécution dans l'interface NetBox.
- Logs globaux NetBox :
  ```bash
  tail -f /opt/netbox/netbox/netbox.log
  ```
- Tester la connexion Proxmox :
  ```bash
  curl -k -H "Authorization: PVEAPIToken=root@pam!netbox-sync=SECRET" https://PROXMOX:8006/api2/json/version
  ```

---

## Résolution des problèmes courants

- Connection timeout / refused :
  - Vérifier accessibilité (ping, curl -k), firewall, port 8006 (ss/telnet).
- 401 Unauthorized :
  - Vérifier format `user@realm!tokenid`, tester le token via curl.
- Conflits d'IP / doublons :
  - Rechercher dans NetBox IPAM et Virtualization → Virtual Machines.
- VMs supprimées par erreur :
  - Désactiver le nettoyage automatique pour investigation, vérifier la présence de la VM dans le cluster Proxmox.
- Lenteur :
  - Installer QEMU Guest Agent sur les VMs, optimiser réseau, désactiver syncs optionnels.

---

## FAQ (sélection)

- Support multi-nœuds : ✅ Le script parcourt tous les nœuds du cluster.
- LXC supportés ? ❌ Non, seules les VMs QEMU/KVM sont supportées.
- Le script modifie Proxmox ? ❌ Non, lecture seule sur Proxmox.
- Que faire si une VM est renommée ? Le script traitera le nouveau nom comme une nouvelle VM — éviter en renommant aussi côté NetBox si possible.

---

## Contribution & support

Signaler un bug : ouvrir une issue avec versions (NetBox, Proxmox), logs (masqués si nécessaire) et étapes de reproduction.

Contribuer :
```text
git fork
git checkout -b feature/amelioration
# modifs
git commit -am "Amélioration README"
git push origin feature/amelioration
# ouvrir une PR
```

---

## Licence

Ce projet est sous licence MIT. Voir le fichier LICENSE.

---

## Changelog (extrait)
- Version 2.0 : ajout du nettoyage automatique, support disques, détection Public/Private, améliorations interface.
- Version 1.0 : synchronisation basique VMs, support QEMU Guest Agent.

---

Made with ❤️ pour la communauté Proxmox & NetBox.  
Documentation : [NetBox](https://docs.netbox.dev/) | [Proxmox API](https://pve.proxmox.com/wiki/Proxmox_VE_API)
