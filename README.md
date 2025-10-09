# 🔄 Proxmox → NetBox Sync Script

Script de synchronisation automatique et intelligent des machines virtuelles Proxmox vers NetBox avec nettoyage automatique des éléments obsolètes.

## ✨ Fonctionnalités

- 🔄 **Synchronisation complète** : VMs, interfaces réseau, adresses IP, disques virtuels et plateformes OS
- 🧹 **Nettoyage automatique** : Suppression des VMs, interfaces et IPs qui n'existent plus dans Proxmox
- 🌐 **Détection intelligente** : Classification automatique Public/Private basée sur les plages IP
- 💾 **Gestion des disques** : Synchronisation détaillée des disques virtuels (taille, type, storage)
- 🔌 **Interfaces réseau** : Support complet avec MAC addresses, VLANs et bridges
- 🤖 **QEMU Guest Agent** : Récupération des informations OS et réseau en temps réel avec fallback automatique
- 🧪 **Mode Dry-Run** : Testez sans risque avant d'appliquer les modifications
- 📊 **Logging détaillé** : Suivi complet de toutes les opérations avec statistiques

## 📋 Prérequis

### Versions requises

| Composant | Version minimale | Version testée | Notes |
|-----------|------------------|----------------|-------|
| **NetBox** | 3.5.0 | 4.0.x | Script compatible avec les versions récentes |
| **Proxmox VE** | 7.0 | 8.x | Support API v2 requis |
| **Python** | 3.8 | 3.11+ | Inclus dans NetBox |

### Dépendances Python

Ces bibliothèques sont généralement déjà installées avec NetBox :

```python
requests>=2.28.0
urllib3>=1.26.0
django>=4.0  # Fourni par NetBox
```

### Permissions NetBox

L'utilisateur NetBox doit avoir les permissions suivantes :

- ✅ `virtualization.add_virtualmachine`
- ✅ `virtualization.change_virtualmachine`
- ✅ `virtualization.delete_virtualmachine` (si nettoyage activé)
- ✅ `virtualization.add_vminterface`
- ✅ `virtualization.change_vminterface`
- ✅ `virtualization.delete_vminterface` (si nettoyage activé)
- ✅ `ipam.add_ipaddress`
- ✅ `ipam.change_ipaddress`
- ✅ `dcim.add_platform`
- ✅ `dcim.change_platform`

## 🚀 Installation

### Étape 1 : Accéder au serveur NetBox

## SSH vers votre serveur NetBox
ssh user@netbox-server

## Passer en utilisateur netbox
sudo su - netbox

## Activer l'environnement virtuel Python
source /opt/netbox/venv/bin/activate

### Étape 2 : Créer le répertoire des scripts

## Naviguer vers le répertoire NetBox
cd /opt/netbox/netbox

## Créer le répertoire pour les scripts personnalisés (s'il n'existe pas)
mkdir -p scripts

# Vérifier les permissions
ls -la scripts/

### Étape 3 : Installer le script

## Télécharger le script
cd /opt/netbox/netbox/scripts
wget https://raw.githubusercontent.com/Steph-ux/proxmox-netbox-sync/main/proxmox_sync.py

## OU copier le fichier manuellement
## Ensuite, définir les permissions appropriées
chmod 644 proxmox_sync.py
chown netbox:netbox proxmox_sync.py

### Étape 4 : Vérifier l'installation

## Retourner au répertoire NetBox
cd /opt/netbox/netbox

## Tester l'import du script
python3 manage.py nbshell
>>> from scripts.proxmox_sync import ProxmoxSync
>>> exit()

### Étape 5 : Redémarrer NetBox (si nécessaire)

## Redémarrer les services NetBox
sudo systemctl restart netbox netbox-rq

## 🔑 Configuration du Token API Proxmox

### Création du token (méthode recommandée)

1. **Connectez-vous à l'interface web Proxmox** : `https://votre-proxmox:8006`

2. **Accédez aux permissions** :
   - Datacenter → Permissions → Users
   - Cliquez sur votre utilisateur (ou créez-en un dédié)

3. **Créer un token API** :
   Datacenter → Permissions → API Tokens → Add
   
   User: root@pam (ou utilisateur personnalisé)
   Token ID: netbox-sync
   ☑️ Privilege Separation: Décoché (ou configurer les permissions)

4. **Notez les informations** (important - ne s'affiche qu'une fois !) :
   Token ID: root@pam!netbox-sync
   Secret: xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx

### Permissions minimales requises

Si vous utilisez "Privilege Separation", créez un rôle avec ces permissions :

# Créer un rôle en lecture seule pour la synchronisation
pveum role add NetBoxSync -privs "VM.Audit,Sys.Audit,Datastore.Audit"

# Assigner le rôle à l'utilisateur/token
pveum acl modify / -user root@pam -role NetBoxSync

**Permissions détaillées** :
- `VM.Audit` : Lire les informations des VMs
- `Sys.Audit` : Lire les informations système
- `Datastore.Audit` : Lire les informations de stockage

### Test du token

# Tester le token avec curl
curl -k -H "Authorization: PVEAPIToken=root@pam!netbox-sync=VOTRE_SECRET" \
  https://votre-proxmox:8006/api2/json/nodes

## Vous devriez voir une réponse JSON avec la liste des nœuds

## 📖 Guide d'utilisation

### Accès au script dans NetBox

1. Connectez-vous à NetBox : `https://votre-netbox`
2. Allez dans **Operations → Integration → Data Source → +Add**
      - Name: "script" (exemple)
      - Type: "local"
      - URL: "/opt/netbox/netbox/scripts" (l'emplacement du répertoire du script)
      - Save
Cliquez ensuite sur votre Data Sources et cliquez sur le bouton **Sync**
3. Allez dans **Customization → Scripts → +Add**
      - Data Source: "script"
      - File: proxmox_sync.py (le script)
5. Trouvez **"Proxmox VM Sync"**
6. Cliquez sur **"Run Script"**

### Configuration de base (première synchronisation)

#### Paramètres obligatoires

| Paramètre | Exemple | Description |
|-----------|---------|-------------|
| **Cluster NetBox** | `Production Cluster` | Cluster où ajouter les VMs |
| **Serveur Proxmox** | `192.168.1.10` ou `pve.local.lan` | IP ou hostname du serveur Proxmox |
| **Token ID** | `root@pam!netbox-sync` | ID du token API |
| **Token Secret** | `xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx` | Secret du token |

#### Paramètres optionnels

| Option | Par défaut | Description |
|--------|------------|-------------|
| **Synchroniser les interfaces** | ✅ Oui | Interfaces réseau et IPs |
| **Synchroniser les plateformes** | ✅ Oui | Informations OS (via QEMU Agent) |
| **Définir IP primaire** | ✅ Oui | Première IP comme IP primaire |
| **Synchroniser type connexion** | ✅ Oui | Détection Public/Private |
| **Synchroniser les disques virtuels** | ✅ Oui | Objets disques dans NetBox |
| **Nettoyer les éléments obsolètes** | ✅ Oui | ⚠️ Supprime ce qui n'existe plus dans Proxmox |

### Exemples d'utilisation

#### Exemple 1 : Synchronisation complète (recommandé)

✅ Tous les paramètres activés
⚠️ ATTENTION : Le nettoyage supprimera les VMs qui n'existent plus dans Proxmox !

Résultat attendu :
- Toutes les VMs Proxmox ajoutées/mises à jour
- Interfaces et IPs synchronisées
- Plateformes OS détectées
- VMs obsolètes supprimées de NetBox

#### Exemple 2 : Synchronisation sans suppression

✅ Tous les paramètres activés
❌ Nettoyer les éléments obsolètes : DÉCOCHE

Résultat attendu :
- Synchronisation normale
- VMs obsolètes conservées dans NetBox
- Permet de vérifier manuellement avant suppression

#### Exemple 3 : Synchronisation VMs uniquement (minimaliste)

✅ Cluster, Proxmox Host, Token
❌ Synchroniser les interfaces : DÉCOCHE
❌ Synchroniser les plateformes : DÉCOCHE
❌ Synchroniser les disques virtuels : DÉCOCHE
❌ Nettoyer les éléments obsolètes : DÉCOCHE

Résultat attendu :
- Seules les informations de base des VMs (nom, CPU, RAM, statut)
- Aucune interface ni IP
- Idéal pour un premier test

#### Exemple 4 : Mode Dry-Run (test sans modification)

✅ Tous les paramètres configurés
⚠️ DÉCOCHER "Commit changes" en bas du formulaire

Résultat attendu :
- Aucune modification dans NetBox
- Logs montrant ce qui SERAIT fait
- Parfait pour valider avant d'appliquer
```

### Workflow recommandé pour la première fois

graph TD
    A[Préparer le cluster NetBox] --> B[Créer token Proxmox]
    B --> C[Mode Dry-Run complet]
    C --> D{Résultats OK ?}
    D -->|Non| E[Ajuster paramètres]
    E --> C
    D -->|Oui| F[Sync sans nettoyage]
    F --> G[Vérifier dans NetBox]
    G --> H{Tout est correct ?}
    H -->|Oui| I[Activer nettoyage]
    H -->|Non| J[Corriger manuellement]
    J --> F

## 🔧 Fonctionnement détaillé

### Flux de synchronisation

1. Connexion à Proxmox
   ├─ Validation du token
   └─ Récupération de la liste des nœuds

2. Pour chaque nœud Proxmox
   ├─ Liste des VMs QEMU
   └─ Pour chaque VM
       ├─ Récupération config (CPU, RAM, disques)
       ├─ Tentative QEMU Agent (OS, IPs)
       ├─ Création/MAJ VM dans NetBox
       ├─ Sync interfaces réseau
       ├─ Sync adresses IP
       ├─ Sync disques virtuels
       ├─ Définition IP primaire
       └─ Détection type connexion

3. Nettoyage (si activé)
   ├─ Comparaison VMs NetBox vs Proxmox
   ├─ Suppression VMs obsolètes
   ├─ Détachement IPs orphelines
   └─ Suppression interfaces obsolètes

### Détection du type de connexion

Le script analyse automatiquement les plages IP pour déterminer le type :

| Plage IP | Type détecté | Critère |
|----------|--------------|---------|
| `10.0.0.0/8` | 🔒 Private | RFC 1918 |
| `172.16.0.0/12` | 🔒 Private | RFC 1918 |
| `192.168.0.0/16` | 🔒 Private | RFC 1918 |
| `127.0.0.0/8` | ❌ Ignoré | Loopback |
| Autres | 🌐 Public | Internet routable |

**Logique** :
- Si au moins une IP publique → `Public`
- Si seulement des IPs privées → `Private`
- Si aucune IP → Non défini

### Gestion des interfaces réseau

#### Avec QEMU Guest Agent (recommandé)

Avantages :
✅ Noms réels des interfaces (eth0, ens18, etc.)
✅ Adresses IP en temps réel
✅ Information précise sur le système d'exploitation
✅ État de connexion des interfaces

Prérequis :
- QEMU Guest Agent installé dans la VM
- Agent démarré et fonctionnel

**Installation QEMU Agent** :

# Debian/Ubuntu
apt-get install qemu-guest-agent
systemctl start qemu-guest-agent

# CentOS/RHEL
yum install qemu-guest-agent
systemctl start qemu-guest-agent

# Windows
# Installer depuis le CD VirtIO drivers ou
# Télécharger depuis https://fedorapeople.org/groups/virt/virtio-win/

#### Sans QEMU Guest Agent (fallback)

Limitations :
⚠️ Noms génériques (net0, net1, etc.)
⚠️ IPs récupérées depuis Proxmox (peuvent être obsolètes)
⚠️ Pas d'information OS détaillée

Le script fonctionne mais avec moins de précision.

## 📊 Interprétation des logs

### Logs de succès

✅ VM myserver créée dans NetBox
  Plateforme assignée: Ubuntu 22.04.3 LTS
  Synchronisation des disques virtuels...
    Disque créé: scsi0 (100.0GB)
    Disque créé: scsi1 (500.0GB)
  Synchronisation des interfaces...
    Interface net0 créée
      MAC AA:BB:CC:DD:EE:FF créée et assignée
      IP 192.168.1.100/24 créée et assignée
  IP primaire définie: 192.168.1.100/24 (interface: net0)
  Type de connexion défini: Private

**Interprétation** : VM synchronisée avec succès, agent QEMU disponible, toutes les données récupérées.

### Logs d'avertissement

⚠️ Agent QEMU non disponible pour VM oldserver - utilisation du fallback
    Interface existante trouvée: net0 (MAC: AA:BB:CC:DD:EE:FF)
    Préservation du nom personnalisé: eth0-internal
⚠️ IP 192.168.1.50/24 déjà assignée à une autre VM (webserver), ignorée

**Interprétation** : 
- VM fonctionne sans agent (normal pour certaines VMs)
- Noms personnalisés préservés (bon comportement)
- Conflit IP détecté (à vérifier manuellement)

### Logs de nettoyage

🗑️  VM obsolète détectée: old-test-vm
    Détachement IP: 192.168.1.200/24
    VM old-test-vm supprimée de NetBox (1 IP(s) détachée(s))

🗑️  Suppression interface obsolète: net2
🗑️  Détachement IP obsolète: 10.0.0.50/24

**Interprétation** : Ressources supprimées automatiquement car elles n'existent plus dans Proxmox.

### Logs d'erreur

❌ Erreur lors de la requête https://proxmox:8006/api2/json/nodes: Connection timeout

**Cause possible** : 
- Serveur Proxmox injoignable
- Firewall bloquant le port 8006
- Token API invalide

## ❓ FAQ et Troubleshooting

### Questions fréquentes

#### Q1 : Le script fonctionne-t-il avec plusieurs nœuds Proxmox ?

**R :** ✅ Oui ! Le script parcourt automatiquement tous les nœuds d'un cluster Proxmox et synchronise toutes les VMs.

---

#### Q2 : Que se passe-t-il si je renomme une VM dans Proxmox ?

**R :** Le script créera une nouvelle VM dans NetBox avec le nouveau nom. L'ancienne sera marquée comme obsolète et supprimée si le nettoyage est activé. Pour éviter cela, renommez aussi manuellement dans NetBox.

---

#### Q3 : Les conteneurs LXC sont-ils supportés ?

**R :** ❌ Non, actuellement seules les VMs QEMU/KVM sont supportées. Les conteneurs LXC utilisent une API différente.

---

#### Q4 : Peut-on exécuter le script automatiquement (cron) ?

**R :** ✅ Oui, via l'API NetBox ou un job planifié :

# Exemple avec l'API NetBox (script externe)
import requests

url = "https://netbox/api/extras/scripts/proxmox_sync.ProxmoxSync/"
headers = {"Authorization": "Token VOTRE_TOKEN_NETBOX"}
data = {
    "data": {
        "target_cluster": 1,
        "proxmox_host": "192.168.1.10",
        "proxmox_token_id": "root@pam!netbox",
        "proxmox_token_secret": "secret",
        "commit": True
    }
}
response = requests.post(url, headers=headers, json=data)

---

#### Q5 : Comment gérer plusieurs clusters Proxmox ?

**R :** Exécutez le script plusieurs fois, une fois par cluster, en sélectionnant un cluster NetBox différent à chaque fois.

---

#### Q6 : Le script modifie-t-il quelque chose dans Proxmox ?

**R :** ❌ Non, le script est en **lecture seule** sur Proxmox. Aucune modification n'est jamais apportée aux VMs Proxmox.

---

#### Q7 : Que faire si une VM a plusieurs IPs et la mauvaise est définie comme primaire ?

**R :** Changez manuellement l'IP primaire dans NetBox. Le script ne la modifiera plus lors des prochaines synchronisations (il ne définit une IP primaire que si aucune n'est déjà définie).

---

### Problèmes courants et solutions

#### ❌ Erreur : "Connection refused" ou "Connection timeout"

**Symptômes** :
❌ Erreur lors de la requête https://proxmox:8006/api2/json/nodes: 
   Connection refused

**Solutions** :
1. Vérifier que Proxmox est accessible :
   ping proxmox-server
   curl -k https://proxmox-server:8006

2. Vérifier le firewall :
   # Sur le serveur NetBox
   telnet proxmox-server 8006

3. Vérifier le port (par défaut 8006) :
   # Sur le serveur Proxmox
   ss -tlnp | grep 8006

---

#### ❌ Erreur : "Authentication failure" ou "401 Unauthorized"

**Symptômes** :
❌ Réponse non-200 : 401 - {"success":0}

**Solutions** :
1. Vérifier le format du Token ID :
   Format correct : user@realm!tokenid
   Exemple : root@pam!netbox-sync
   
   ❌ Incorrect : root@pam
   ❌ Incorrect : netbox-sync

2. Vérifier que le token existe :
   # Dans Proxmox Web UI
   Datacenter → Permissions → API Tokens

3. Tester le token manuellement :
   curl -k -H "Authorization: PVEAPIToken=root@pam!netbox-sync=SECRET" \
     https://proxmox:8006/api2/json/version

---

#### ❌ Erreur : "VM already exists" ou problèmes de duplication

**Symptômes** :
❌ IntegrityError: duplicate key value violates unique constraint

**Solutions** :
1. Vérifier qu'il n'y a pas de doublon dans NetBox :
   
   Virtualization → Virtual Machines → Filtrer par nom

3. Supprimer manuellement les doublons ou utiliser un autre cluster NetBox

4. En dernier recours, supprimer toutes les VMs du cluster et resynchroniser

---

#### ⚠️ Avertissement : "Agent QEMU non disponible"

**Symptômes** :
⚠️ Agent QEMU non disponible pour VM myserver - utilisation du fallback

**Impact** :
- Noms d'interfaces génériques (net0, net1 au lieu de eth0, ens18)
- Informations OS non disponibles
- IPs potentiellement obsolètes

**Solutions** :
1. Installer QEMU Guest Agent dans la VM :
   # Debian/Ubuntu
   apt install qemu-guest-agent
   systemctl enable --now qemu-guest-agent
   
   # CentOS/RHEL
   yum install qemu-guest-agent
   systemctl enable --now qemu-guest-agent

2. Activer l'agent dans Proxmox :
   VM → Options → QEMU Guest Agent → Enabled ✅

3. Redémarrer la VM ou attendre quelques minutes
   
---

#### ❌ Erreur : "Permission denied" lors de l'import du script

**Symptômes** :
ImportError: No module named 'scripts.proxmox_sync'

**Solutions** :
1. Vérifier l'emplacement du fichier :
   ```bash
   ls -la /opt/netbox/netbox/scripts/proxmox_sync.py
   ```

2. Vérifier les permissions :
   chown netbox:netbox /opt/netbox/netbox/scripts/proxmox_sync.py
   chmod 644 /opt/netbox/netbox/scripts/proxmox_sync.py

3. Redémarrer NetBox :
   sudo systemctl restart netbox netbox-rq

---

#### ⚠️ Les IPs ne sont pas mises à jour

**Symptômes** :
Les IPs dans NetBox ne correspondent pas à celles dans Proxmox/VMs.

**Causes possibles** :
1. **Agent QEMU non installé** → Le script utilise des données en cache de Proxmox
2. **IP déjà assignée à une autre VM** → Le script ignore les conflits pour éviter les erreurs

**Solutions** :
1. Installer QEMU Guest Agent (voir ci-dessus)
2. Vérifier les conflits d'IP :

   IPAM → IP Addresses → Rechercher l'IP

3. Détacher manuellement l'IP de l'ancien équipement si nécessaire

---

#### 🗑️ Des VMs sont supprimées par erreur

**Symptômes** :
Des VMs valides sont supprimées de NetBox après synchronisation.

**Causes possibles** :
1. La VM n'existe plus dans Proxmox
2. La VM est sur un autre nœud/cluster Proxmox
3. Erreur de connexion pendant la récupération des VMs

**Solutions** :
1. **Désactiver temporairement le nettoyage** :

   ❌ Nettoyer les éléments obsolètes : DÉCOCHER

2. Vérifier dans Proxmox :
   # Lister toutes les VMs
   pvesh get /cluster/resources --type vm

3. Vérifier les logs du script pour identifier la cause

---

#### 🐌 Le script est très lent

**Symptômes** :
La synchronisation prend plusieurs minutes pour quelques VMs.

**Causes** :
- Nombreuses requêtes API vers Proxmox
- VMs sans agent QEMU (timeouts multiples)
- Réseau lent entre NetBox et Proxmox

**Solutions** :
1. Installer QEMU Agent sur toutes les VMs
2. Optimiser la connexion réseau
3. Désactiver les synchronisations optionnelles :

   ❌ Synchroniser les plateformes (si pas critique)
   ❌ Synchroniser les disques virtuels (si pas nécessaire)

---

### 🔍 Déboguer les problèmes

#### Activer les logs détaillés

Le script affiche déjà beaucoup d'informations, mais vous pouvez consulter :


# Logs NetBox généraux
tail -f /opt/netbox/netbox/netbox.log

# Logs de l'exécution des scripts
# Visibles directement dans l'interface web NetBox après exécution


#### Tester la connexion Proxmox manuellement


# Test de connectivité
curl -k https://PROXMOX_IP:8006/api2/json/version

# Test avec authentification
curl -k -H "Authorization: PVEAPIToken=USER@REALM!TOKENID=SECRET" \
  https://PROXMOX_IP:8006/api2/json/nodes

# Test récupération VMs
curl -k -H "Authorization: PVEAPIToken=USER@REALM!TOKENID=SECRET" \
  https://PROXMOX_IP:8006/api2/json/nodes/NODE_NAME/qemu

#### Mode Dry-Run approfondi

Toujours tester avec le mode Dry-Run avant toute modification importante :

1. Décocher "Commit changes"
2. Exécuter le script
3. Analyser les logs ligne par ligne
4. Vérifier que tout est correct
5. Réexécuter avec "Commit changes" ✅

## 📞 Support et contribution

### Signaler un bug

Ouvrez une issue GitHub avec :
- Version de NetBox
- Version de Proxmox
- Description du problème
- Logs du script (masquez les informations sensibles)
- Étapes pour reproduire

### Contribuer

Les contributions sont les bienvenues ! 

1. Fork le projet
2. Créez une branche (`git checkout -b feature/amelioration`)
3. Committez vos changements (`git commit -am 'Ajout nouvelle fonctionnalité'`)
4. Push vers la branche (`git push origin feature/amelioration`)
5. Ouvrez une Pull Request

## 📄 Licence

Ce projet est sous licence MIT. Voir le fichier [LICENSE](LICENSE) pour plus de détails.

## 🙏 Remerciements

- Équipe Proxmox pour leur excellente API
- Équipe NetBox pour le framework de scripts
- La communauté pour les retours et améliorations

## 📝 Changelog

### Version 2.0 (Actuelle)
- ✨ Ajout du nettoyage automatique des éléments obsolètes
- ✨ Support complet des disques virtuels
- ✨ Détection automatique du type de connexion (Public/Private)
- 🐛 Correction de la gestion des adresses MAC
- 🐛 Amélioration de la gestion des interfaces avec noms personnalisés
- 📝 Documentation complète

### Version 1.0
- 🎉 Version initiale
- ✨ Synchronisation basique des VMs
- ✨ Support QEMU Guest Agent

---

**Made with ❤️ for the Proxmox & NetBox community**

Pour plus d'informations : [Documentation NetBox](https://docs.netbox.dev/) | [Documentation Proxmox API](https://pve.proxmox.com/wiki/Proxmox_VE_API)
