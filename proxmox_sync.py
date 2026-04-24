from extras.scripts import *
from virtualization.models import Cluster, ClusterType, VirtualMachine, VMInterface, VirtualDisk
from dcim.models import Platform
from ipam.models import IPAddress
from django.contrib.contenttypes.models import ContentType
from django.db import IntegrityError, transaction
import requests
import urllib3
import json
import re
import ipaddress


class ProxmoxSync(Script):
    class Meta:
        name = "Proxmox VM Sync"
        description = "Synchronise les VMs Proxmox vers NetBox avec nettoyage automatique des elements obsoletes"
        commit_default = True
        field_order = [
            'target_cluster', 'proxmox_host', 'proxmox_token_id', 'proxmox_token_secret',
            'sync_interfaces', 'sync_platforms', 'set_primary_ip', 'sync_connection_type',
            'sync_virtual_disks', 'cleanup_obsolete'
        ]

    target_cluster = ObjectVar(
        description="Selectionnez le cluster NetBox ou ajouter les VMs",
        model=Cluster,
        label="Cluster NetBox",
        required=True
    )

    proxmox_host = StringVar(
        description="Adresse du serveur Proxmox",
        label="Serveur Proxmox",
        required=True
    )

    proxmox_token_id = StringVar(
        description="ID du token API (user@pve!token)",
        label="Token ID",
        required=True
    )

    proxmox_token_secret = StringVar(
        description="Secret du token API",
        label="Token Secret",
        required=True
    )

    sync_interfaces = BooleanVar(
        description="Synchroniser les interfaces reseau et adresses IP",
        label="Synchroniser les interfaces",
        default=True
    )

    sync_platforms = BooleanVar(
        description="Synchroniser les informations de plateforme OS",
        label="Synchroniser les plateformes",
        default=True
    )

    # FIX #1 : BooleanVar conserve son nom de champ.
    # La methode associee est renommee apply_primary_ip() plus bas.
    set_primary_ip = BooleanVar(
        description="Definir automatiquement la premiere IP comme IP primaire",
        label="Definir IP primaire",
        default=True
    )

    sync_connection_type = BooleanVar(
        description="Detecter automatiquement le type de connexion (Private/Public) base sur les IPs",
        label="Synchroniser type connexion",
        default=True
    )

    sync_virtual_disks = BooleanVar(
        description="Synchroniser les disques virtuels comme objets NetBox separes",
        label="Synchroniser les disques virtuels",
        default=True
    )

    cleanup_obsolete = BooleanVar(
        description="ATTENTION: Supprimer automatiquement les VMs, interfaces et IPs qui n'existent plus dans Proxmox",
        label="Nettoyer les elements obsoletes",
        default=True
    )

    # -------------------------------------------------------------------------
    # API Proxmox
    # -------------------------------------------------------------------------

    def proxmox_get(self, url, headers):
        """Effectue une requete GET vers l'API Proxmox avec logs detailles"""
        try:
            self.log_info(f"Requete API : {url}")
            response = requests.get(url, headers=headers, verify=False, timeout=30)
            self.log_info(f"Code reponse : {response.status_code}")

            if response.status_code == 200:
                return response.json()
            else:
                self.log_warning(f"Reponse non-200 : {response.status_code} - {response.text}")
            return None
        except Exception as e:
            self.log_failure(f"Erreur lors de la requete {url}: {str(e)}")
            return None

    # -------------------------------------------------------------------------
    # Parsing
    # -------------------------------------------------------------------------

    def parse_mac_address(self, mac_str):
        """Parse et formate une adresse MAC de maniere normalisee"""
        if not mac_str:
            return None
        mac_clean = re.sub(r'[^a-fA-F0-9]', '', mac_str.lower())
        if len(mac_clean) == 12:
            return ':'.join(mac_clean[i:i+2] for i in range(0, 12, 2)).upper()
        return None

    def parse_proxmox_network_config(self, config_data):
        """Parse la configuration reseau Proxmox"""
        interfaces = []

        for key, value in config_data.items():
            if key.startswith('net'):
                interface_data = {
                    'name': key,
                    'mac_address': None,
                    'bridge': None,
                    'vlan': None,
                    'model': None
                }

                parts = value.split(',')
                for part in parts:
                    if '=' in part:
                        param, val = part.split('=', 1)
                        param = param.strip()
                        val = val.strip()

                        if param in ['virtio', 'e1000', 'rtl8139', 'vmxnet3']:
                            interface_data['model'] = param
                            interface_data['mac_address'] = self.parse_mac_address(val)
                        elif param == 'bridge':
                            interface_data['bridge'] = val
                        elif param == 'tag':
                            try:
                                interface_data['vlan'] = int(val)
                            except ValueError:
                                pass

                if interface_data['mac_address']:
                    interfaces.append(interface_data)

        return interfaces

    def parse_proxmox_disk_config(self, config_data):
        """Parse la configuration des disques Proxmox pour obtenir la taille reelle"""
        total_disk_gb = 0
        disk_details = []

        disk_types = ['scsi', 'ide', 'sata', 'virtio', 'efidisk', 'tpmstate']

        for key, value in config_data.items():
            for disk_type in disk_types:
                if key.startswith(disk_type) and key[len(disk_type):].isdigit():
                    try:
                        parts = value.split(',')
                        disk_size_gb = 0
                        disk_info = {
                            'key': key,
                            'type': disk_type,
                            'size_gb': 0,
                            'storage': 'unknown'
                        }

                        for part in parts:
                            part = part.strip()

                            if ':' in part and '=' not in part:
                                disk_info['storage'] = part.split(':')[0]

                            if part.startswith('size='):
                                size_str = part.replace('size=', '').upper()

                                if size_str.endswith('G'):
                                    disk_size_gb = float(size_str[:-1])
                                elif size_str.endswith('M'):
                                    disk_size_gb = float(size_str[:-1]) / 1024
                                elif size_str.endswith('K'):
                                    disk_size_gb = float(size_str[:-1]) / 1024 / 1024
                                elif size_str.endswith('T'):
                                    disk_size_gb = float(size_str[:-1]) * 1024
                                else:
                                    disk_size_gb = float(size_str) / 1024 / 1024 / 1024

                        if disk_size_gb > 0:
                            disk_info['size_gb'] = disk_size_gb
                            total_disk_gb += disk_size_gb
                            disk_details.append(disk_info)
                            self.log_info(
                                f"      Disque trouve: {key} = {disk_size_gb:.1f}GB ({disk_info['storage']})"
                            )

                    except Exception as e:
                        self.log_warning(f"      Erreur parsing disque {key}: {str(e)}")

        return int(total_disk_gb), disk_details

    # -------------------------------------------------------------------------
    # Agent QEMU
    # -------------------------------------------------------------------------

    def get_vm_os_info(self, base_url, node_name, vm_id, headers):
        """Recupere les informations OS via l'agent QEMU"""
        try:
            os_info_data = self.proxmox_get(
                f'{base_url}/nodes/{node_name}/qemu/{vm_id}/agent/get-osinfo', headers
            )
            if os_info_data and 'data' in os_info_data and 'result' in os_info_data['data']:
                return os_info_data['data']['result']
            return None
        except Exception as e:
            self.log_warning(f"      Agent OS info non disponible: {str(e)}")
            return None

    def get_vm_network_interfaces(self, base_url, node_name, vm_id, headers):
        """Recupere les interfaces reseau via l'agent QEMU"""
        try:
            network_data = self.proxmox_get(
                f'{base_url}/nodes/{node_name}/qemu/{vm_id}/agent/network-get-interfaces', headers
            )
            if network_data and 'data' in network_data and 'result' in network_data['data']:
                return network_data['data']['result']
            return None
        except Exception as e:
            self.log_warning(f"      Agent network info non disponible: {str(e)}")
            return None

    def get_vm_network_status_fallback(self, vm_status_data):
        """
        Recupere les adresses IP depuis le status de la VM (methode fallback).
        Retourne une liste de dicts avec 'address', 'interface' (nom OS) et 'mac_address'.
        """
        ip_addresses = []

        if 'agent-netinfo' in vm_status_data:
            netinfo = vm_status_data['agent-netinfo']
            if 'result' in netinfo:
                for interface in netinfo['result']:
                    if 'ip-addresses' in interface:
                        mac = self.parse_mac_address(interface.get('hardware-address', ''))
                        for ip_info in interface['ip-addresses']:
                            ip_addr = ip_info.get('ip-address')
                            prefix = ip_info.get('prefix', 24)
                            ip_type = ip_info.get('ip-address-type', 'ipv4')

                            if ip_addr and ip_type == 'ipv4' and not ip_addr.startswith('127.'):
                                ip_addresses.append({
                                    'address': f"{ip_addr}/{prefix}",
                                    'interface': interface.get('name', 'unknown'),
                                    'mac_address': mac
                                })

        return ip_addresses

    # -------------------------------------------------------------------------
    # Plateforme
    # -------------------------------------------------------------------------

    def create_or_get_platform(self, os_info, commit):
        """Cree ou recupere une plateforme basee sur les infos OS"""
        if not os_info or not commit:
            return None

        platform_name = None
        try:
            platform_name = os_info.get('pretty-name') or os_info.get('name', 'Unknown OS')

            platform = Platform.objects.filter(name=platform_name).first()

            if not platform:
                platform_slug = re.sub(r'[^a-zA-Z0-9\-_]', '-', platform_name.lower())[:50]

                base_slug = platform_slug
                counter = 1
                while Platform.objects.filter(slug=platform_slug).exists():
                    platform_slug = f"{base_slug}-{counter}"[:50]
                    counter += 1

                platform_data = {
                    'name': platform_name,
                    'slug': platform_slug,
                }

                description_parts = []
                if os_info.get('version'):
                    description_parts.append(f"Version: {os_info['version']}")
                if os_info.get('kernel-release'):
                    description_parts.append(f"Kernel: {os_info['kernel-release']}")
                if os_info.get('machine'):
                    description_parts.append(f"Architecture: {os_info['machine']}")

                if description_parts:
                    platform_data['description'] = " | ".join(description_parts)

                platform = Platform.objects.create(**platform_data)
                self.log_success(f"    Plateforme creee: {platform_name}")
            else:
                self.log_info(f"    Plateforme existante: {platform_name}")

            return platform

        except Exception as e:
            self.log_failure(
                f"    Erreur creation plateforme "
                f"{platform_name if platform_name else 'unknown'}: {str(e)}"
            )
            import traceback
            self.log_debug(f"    Traceback: {traceback.format_exc()}")
            return None

    # -------------------------------------------------------------------------
    # Interfaces
    # -------------------------------------------------------------------------

    def find_interface_by_mac(self, netbox_vm, mac_address):
        """Trouve une interface par son adresse MAC normalisee"""
        if not mac_address:
            return None

        try:
            from dcim.models import MACAddress

            normalized_mac = self.parse_mac_address(mac_address)
            if not normalized_mac:
                return None

            mac_obj = MACAddress.objects.filter(mac_address=normalized_mac).first()
            if mac_obj:
                if (mac_obj.assigned_object_type and
                        hasattr(mac_obj.assigned_object, 'virtual_machine') and
                        mac_obj.assigned_object.virtual_machine == netbox_vm):
                    return mac_obj.assigned_object

            vminterface_ct = ContentType.objects.get_for_model(VMInterface)
            for interface in VMInterface.objects.filter(virtual_machine=netbox_vm):
                interface_mac_obj = MACAddress.objects.filter(
                    assigned_object_type=vminterface_ct,
                    assigned_object_id=interface.pk
                ).first()

                if interface_mac_obj:
                    mac_str = str(interface_mac_obj.mac_address).upper().replace('-', ':')
                    if mac_str == normalized_mac.upper():
                        return interface

            return None
        except Exception as e:
            self.log_warning(f"    Erreur recherche interface par MAC {mac_address}: {str(e)}")
            return None

    def sync_vm_interfaces(self, netbox_vm, vm_config, base_url, node_name, vm_id, headers, vm_status, commit):
        """Synchronise les interfaces d'une VM avec nettoyage des interfaces obsoletes"""
        if not vm_config or 'data' not in vm_config:
            return

        config_data = vm_config['data']
        interfaces_config = self.parse_proxmox_network_config(config_data)

        agent_interfaces = self.get_vm_network_interfaces(base_url, node_name, vm_id, headers)

        ip_addresses = []
        agent_available = False

        if agent_interfaces:
            agent_available = True
            self.log_success(f"  Agent QEMU disponible - utilisation des donnees agent")

            for agent_interface in agent_interfaces:
                interface_name = agent_interface.get('name', 'unknown')
                mac_address = self.parse_mac_address(agent_interface.get('hardware-address', ''))

                if 'ip-addresses' in agent_interface:
                    for ip_info in agent_interface['ip-addresses']:
                        ip_addr = ip_info.get('ip-address')
                        prefix = ip_info.get('prefix', 24)
                        ip_type = ip_info.get('ip-address-type', 'ipv4')

                        if ip_addr and ip_type == 'ipv4' and not ip_addr.startswith('127.'):
                            ip_addresses.append({
                                'address': f"{ip_addr}/{prefix}",
                                'interface': interface_name,
                                'mac_address': mac_address
                            })
        else:
            self.log_warning(
                f"  Agent QEMU non disponible pour VM "
                f"{netbox_vm.name if netbox_vm else vm_id} - utilisation du fallback"
            )
            status_data = vm_status.get('data', {}) if vm_status else {}
            ip_addresses = self.get_vm_network_status_fallback(status_data)

        if not commit:
            self.log_info(f"  [DRY-RUN] Interfaces trouvees: {len(interfaces_config)}")
            self.log_info(f"  [DRY-RUN] IPs trouvees: {len(ip_addresses)}")
            self.log_info(f"  [DRY-RUN] Agent QEMU: {'Disponible' if agent_available else 'Non disponible'}")
            return

        synced_interface_macs = set()
        synced_interface_names = set()

        for interface_config in interfaces_config:
            try:
                mac_address = interface_config['mac_address']
                interface_name = interface_config['name']

                if mac_address:
                    synced_interface_macs.add(mac_address.upper())
                synced_interface_names.add(interface_name)

                existing_interface = self.find_interface_by_mac(netbox_vm, mac_address)

                interface_data = {
                    'virtual_machine': netbox_vm,
                    'enabled': True
                }

                desc_parts = []
                if interface_config['model']:
                    desc_parts.append(f"Model: {interface_config['model']}")
                if interface_config['bridge']:
                    desc_parts.append(f"Bridge: {interface_config['bridge']}")
                if interface_config['vlan']:
                    desc_parts.append(f"VLAN: {interface_config['vlan']}")
                if mac_address:
                    desc_parts.append(f"MAC: {mac_address}")
                desc_parts.append("Source: QEMU Agent" if agent_available else "Source: Proxmox Config (Agent non disponible)")

                new_description = " | ".join(desc_parts) if desc_parts else ""

                if existing_interface:
                    self.log_info(
                        f"    Interface existante trouvee: {existing_interface.name} (MAC: {mac_address})"
                    )

                    if existing_interface.name.startswith('net'):
                        interface_data['name'] = interface_config['name']
                    else:
                        interface_data['name'] = existing_interface.name
                        self.log_info(f"      Preservation du nom personnalise: {existing_interface.name}")

                    if existing_interface.description:
                        if not any(kw in existing_interface.description.lower()
                                   for kw in ['model:', 'bridge:', 'vlan:', 'source:']):
                            interface_data['description'] = (
                                f"{existing_interface.description} | {new_description}"
                                if new_description else existing_interface.description
                            )
                        else:
                            interface_data['description'] = new_description
                    else:
                        interface_data['description'] = new_description

                    for key, value in interface_data.items():
                        if value is not None:
                            setattr(existing_interface, key, value)
                    existing_interface.save()
                    netbox_interface = existing_interface
                    self.log_success(f"    Interface {netbox_interface.name} mise a jour")

                else:
                    interface_data['name'] = interface_config['name']
                    interface_data['description'] = new_description

                    existing_by_name = VMInterface.objects.filter(
                        virtual_machine=netbox_vm,
                        name=interface_config['name']
                    ).first()

                    if existing_by_name:
                        self.log_warning(
                            f"    Interface {interface_config['name']} existe deja, mise a jour..."
                        )
                        for key, value in interface_data.items():
                            if key != 'name' and value is not None:
                                setattr(existing_by_name, key, value)
                        existing_by_name.save()
                        netbox_interface = existing_by_name
                        self.log_success(f"    Interface {interface_config['name']} mise a jour")
                    else:
                        netbox_interface = VMInterface.objects.create(**interface_data)
                        self.log_success(f"    Interface {interface_data['name']} creee")

                if mac_address:
                    self.assign_mac_to_interface(netbox_interface, mac_address)

                # FIX #2 + #3 : sync_interface_ips utilise desormais la MAC
                # pour matcher dans les deux modes (agent et fallback).
                self.sync_interface_ips(netbox_interface, ip_addresses, mac_address, agent_available)

            except Exception as e:
                self.log_failure(f"    Erreur interface {interface_config.get('name', 'unknown')}: {str(e)}")

        # Nettoyage des interfaces obsoletes
        if self.cleanup_obsolete:
            try:
                vminterface_ct = ContentType.objects.get_for_model(VMInterface)
                all_vm_interfaces = VMInterface.objects.filter(virtual_machine=netbox_vm)
                for interface in all_vm_interfaces:
                    should_delete = False

                    if interface.name.startswith('net'):
                        if interface.name not in synced_interface_names:
                            should_delete = True
                    else:
                        try:
                            from dcim.models import MACAddress
                            mac_obj = MACAddress.objects.filter(
                                assigned_object_type=vminterface_ct,
                                assigned_object_id=interface.pk
                            ).first()

                            if mac_obj:
                                mac_str = str(mac_obj.mac_address).upper().replace('-', ':')
                                if mac_str not in synced_interface_macs:
                                    should_delete = True
                        except Exception as mac_error:
                            self.log_warning(
                                f"    Erreur verification MAC pour {interface.name}: {str(mac_error)}"
                            )

                    if should_delete:
                        self.log_warning(f"    Suppression interface obsolete: {interface.name}")
                        interface.delete()
            except Exception as cleanup_error:
                self.log_warning(f"    Erreur nettoyage interfaces: {str(cleanup_error)}")

    def assign_mac_to_interface(self, netbox_interface, mac_address):
        """Assigne une adresse MAC a une interface en utilisant le modele MACAddress"""
        try:
            from dcim.models import MACAddress

            normalized_mac = self.parse_mac_address(mac_address)
            if not normalized_mac:
                return

            mac_obj = MACAddress.objects.filter(mac_address=normalized_mac).first()
            interface_content_type = ContentType.objects.get_for_model(VMInterface)

            if mac_obj:
                if (mac_obj.assigned_object_type == interface_content_type and
                        mac_obj.assigned_object_id == netbox_interface.pk):
                    self.log_info(f"      MAC {normalized_mac} deja assignee a cette interface")
                    return
                elif mac_obj.assigned_object:
                    self.log_warning(
                        f"      MAC {normalized_mac} reassignee a l'interface {netbox_interface.name}"
                    )

                mac_obj.assigned_object_type = interface_content_type
                mac_obj.assigned_object_id = netbox_interface.pk
                mac_obj.save()
            else:
                MACAddress.objects.create(
                    mac_address=normalized_mac,
                    assigned_object_type=interface_content_type,
                    assigned_object_id=netbox_interface.pk
                )
                self.log_success(f"      MAC {normalized_mac} creee et assignee")

        except Exception as e:
            self.log_warning(f"      Erreur assignation MAC {mac_address}: {str(e)}")

    def sync_interface_ips(self, netbox_interface, ip_addresses, interface_mac, agent_available):
        """
        Synchronise les adresses IP d'une interface.

        FIX #2 : le matching se fait TOUJOURS par MAC (normalisee), que l'agent
        soit disponible ou non. L'ancien fallback par nom d'interface OS ("eth0"
        vs "net0") ne pouvait jamais matcher et laissait toutes les IPs orphelines.

        FIX #3 : la verification d'assignation compare desormais assigned_object_type
        ET assigned_object_id pour eviter des faux positifs sur des IDs identiques
        appartenant a des objets de types differents.
        """
        vminterface_ct = ContentType.objects.get_for_model(VMInterface)

        # Matcher les IPs de cette interface par MAC dans tous les cas
        interface_ips = []
        if interface_mac:
            normalized_interface_mac = self.parse_mac_address(interface_mac)
            if normalized_interface_mac:
                interface_ips = [
                    ip for ip in ip_addresses
                    if self.parse_mac_address(ip.get('mac_address') or '') == normalized_interface_mac
                ]

        if not interface_ips:
            self.log_info(f"      Aucune IP trouvee pour l'interface {netbox_interface.name}")

            if self.cleanup_obsolete:
                existing_ips = IPAddress.objects.filter(
                    assigned_object_type=vminterface_ct,
                    assigned_object_id=netbox_interface.pk
                )
                for ip in existing_ips:
                    self.log_warning(f"      Detachement IP obsolete: {ip.address}")
                    ip.assigned_object_type = None
                    ip.assigned_object_id = None
                    ip.status = 'deprecated'
                    ip.save()
            return

        synced_ip_addresses = set()
        for ip_info in interface_ips:
            try:
                synced_ip_addresses.add(str(ipaddress.ip_interface(ip_info['address'])))
            except ValueError:
                self.log_warning(f"      IP invalide ignoree: {ip_info['address']}")

        for ip_info in interface_ips:
            try:
                ip_address = str(ipaddress.ip_interface(ip_info['address']))
                existing_ip = IPAddress.objects.filter(address=ip_address).first()

                if existing_ip:
                    # FIX #3 : verifier les deux champs pour confirmer l'assignation
                    if (existing_ip.assigned_object_type == vminterface_ct and
                            existing_ip.assigned_object_id == netbox_interface.pk):
                        existing_ip.status = 'active'
                        existing_ip.save()
                        self.log_info(f"      IP {ip_address} deja assignee a cette interface")

                    elif existing_ip.assigned_object:
                        if hasattr(existing_ip.assigned_object, 'virtual_machine'):
                            other_vm = existing_ip.assigned_object.virtual_machine
                            if other_vm == netbox_interface.virtual_machine:
                                existing_ip.assigned_object_type = vminterface_ct
                                existing_ip.assigned_object_id = netbox_interface.pk
                                existing_ip.status = 'active'
                                existing_ip.save()
                                self.log_warning(
                                    f"      IP {ip_address} reassignee de "
                                    f"{existing_ip.assigned_object.name} vers {netbox_interface.name}"
                                )
                            else:
                                self.log_warning(
                                    f"      IP {ip_address} deja assignee a une autre VM "
                                    f"({other_vm.name}), ignoree"
                                )
                                continue
                        else:
                            self.log_warning(
                                f"      IP {ip_address} deja assignee a un equipement physique, ignoree"
                            )
                            continue
                    else:
                        existing_ip.assigned_object_type = vminterface_ct
                        existing_ip.assigned_object_id = netbox_interface.pk
                        existing_ip.status = 'active'
                        existing_ip.save()
                        self.log_success(f"      IP {ip_address} assignee a l'interface")

                else:
                    IPAddress.objects.create(
                        address=ip_address,
                        assigned_object_type=vminterface_ct,
                        assigned_object_id=netbox_interface.pk,
                        status='active'
                    )
                    self.log_success(f"      IP {ip_address} creee et assignee")

            except Exception as e:
                self.log_failure(f"      Erreur IP {ip_info.get('address', 'unknown')}: {str(e)}")

        # Nettoyage des IPs obsoletes sur cette interface
        if self.cleanup_obsolete:
            existing_ips = IPAddress.objects.filter(
                assigned_object_type=vminterface_ct,
                assigned_object_id=netbox_interface.pk
            )
            for ip in existing_ips:
                try:
                    normalized_existing = str(ipaddress.ip_interface(str(ip.address)))
                    if normalized_existing not in synced_ip_addresses:
                        self.log_warning(f"      Detachement IP obsolete: {ip.address}")
                        ip.assigned_object_type = None
                        ip.assigned_object_id = None
                        ip.status = 'deprecated'
                        ip.save()
                except ValueError:
                    self.log_warning(f"      IP invalide detectee: {ip.address}")

    # -------------------------------------------------------------------------
    # IP primaire
    # FIX #1 : renommee apply_primary_ip pour eviter la collision avec le BooleanVar.
    # -------------------------------------------------------------------------

    def apply_primary_ip(self, netbox_vm, commit):
        """Definit la premiere IP active comme IP primaire si aucune n'est definie"""
        if not commit:
            self.log_info(
                f"  [DRY-RUN] Verification IP primaire pour {netbox_vm.name if netbox_vm else 'VM'}"
            )
            return

        if netbox_vm.primary_ip4:
            self.log_info(f"  IP primaire deja definie: {netbox_vm.primary_ip4.address}")
            return

        try:
            vminterface_ct = ContentType.objects.get_for_model(VMInterface)
            interfaces = VMInterface.objects.filter(virtual_machine=netbox_vm).order_by('name')

            for interface in interfaces:
                interface_ips = IPAddress.objects.filter(
                    assigned_object_type=vminterface_ct,
                    assigned_object_id=interface.pk,
                    status='active'
                ).order_by('address')

                if interface_ips.exists():
                    first_ip = interface_ips.first()
                    netbox_vm.primary_ip4 = first_ip
                    netbox_vm.save()
                    self.log_success(
                        f"  IP primaire definie: {first_ip.address} (interface: {interface.name})"
                    )
                    return

            self.log_info(f"  Aucune IP active trouvee pour definir comme primaire")

        except Exception as e:
            self.log_warning(f"  Erreur lors de la definition de l'IP primaire: {str(e)}")

    # -------------------------------------------------------------------------
    # Type de connexion
    # -------------------------------------------------------------------------

    def is_private_ip(self, ip_address):
        """Determine si une adresse IP est privee"""
        try:
            ip_str = ip_address.split('/')[0]
            return ipaddress.ip_address(ip_str).is_private
        except ValueError:
            return True

    def determine_connection_type(self, vm_interfaces):
        """Determine le type de connexion base sur les IPs de la VM"""
        has_public = False
        has_private = False
        vminterface_ct = ContentType.objects.get_for_model(VMInterface)

        try:
            for interface in vm_interfaces:
                interface_ips = IPAddress.objects.filter(
                    assigned_object_type=vminterface_ct,
                    assigned_object_id=interface.pk,
                    status='active'
                )

                for ip_obj in interface_ips:
                    ip_address = str(ip_obj.address)

                    if ip_address.startswith('127.'):
                        continue

                    if self.is_private_ip(ip_address):
                        has_private = True
                        self.log_info(f"      IP privee detectee: {ip_address}")
                    else:
                        has_public = True
                        self.log_info(f"      IP publique detectee: {ip_address}")

            if has_public:
                return "Public"
            elif has_private:
                return "Private"
            return None

        except Exception as e:
            self.log_warning(f"      Erreur detection type connexion: {str(e)}")
            return None

    def set_connection_type(self, netbox_vm, commit):
        """Definit le champ Server_Connection_Type base sur les IPs"""
        if not commit:
            self.log_info(
                f"  [DRY-RUN] Detection type de connexion pour "
                f"{netbox_vm.name if netbox_vm else 'VM'}"
            )
            return

        try:
            vm_interfaces = VMInterface.objects.filter(virtual_machine=netbox_vm)

            if not vm_interfaces.exists():
                self.log_info(f"  Aucune interface trouvee pour determiner le type de connexion")
                return

            connection_type = self.determine_connection_type(vm_interfaces)

            if connection_type:
                current_type = netbox_vm.custom_field_data.get('Server_Connection_Type')

                if current_type != connection_type:
                    if not netbox_vm.custom_field_data:
                        netbox_vm.custom_field_data = {}
                    netbox_vm.custom_field_data['Server_Connection_Type'] = connection_type
                    netbox_vm.save()
                    self.log_success(f"  Type de connexion defini: {connection_type}")
                else:
                    self.log_info(f"  Type de connexion deja correct: {connection_type}")
            else:
                self.log_info(f"  Impossible de determiner le type de connexion (aucune IP valide)")

        except Exception as e:
            self.log_warning(f"  Erreur lors de la definition du type de connexion: {str(e)}")

    # -------------------------------------------------------------------------
    # Disques virtuels
    # -------------------------------------------------------------------------

    def sync_vm_virtual_disks(self, netbox_vm, disk_details, commit):
        """Synchronise les disques virtuels d'une VM avec nettoyage des disques obsoletes"""
        proxmox_prefixes = ['scsi', 'ide', 'sata', 'virtio', 'efidisk', 'tpmstate']

        if not disk_details:
            if commit and self.cleanup_obsolete and netbox_vm:
                existing_disks = VirtualDisk.objects.filter(virtual_machine=netbox_vm)
                for disk in existing_disks:
                    if any(disk.name.startswith(p) for p in proxmox_prefixes):
                        disk.delete()
                        self.log_warning(
                            f"    Disque {disk.name} supprime (n'existe plus dans Proxmox)"
                        )
            return

        if not commit:
            self.log_info(f"  [DRY-RUN] {len(disk_details)} disques seraient synchronises")
            for disk_info in disk_details:
                self.log_info(
                    f"    - {disk_info['key']}: {disk_info['size_gb']:.1f}GB ({disk_info['storage']})"
                )
            return

        try:
            existing_disks = {
                disk.name: disk
                for disk in VirtualDisk.objects.filter(virtual_machine=netbox_vm)
            }
            current_disk_names = set()

            for disk_info in disk_details:
                disk_name = disk_info['key']
                current_disk_names.add(disk_name)

                size_mb = int(disk_info['size_gb'] * 1024)
                description = (
                    f"Storage: {disk_info['storage']} | "
                    f"Type: {disk_info['type']} | "
                    f"Size: {disk_info['size_gb']:.1f}GB"
                )

                if disk_name in existing_disks:
                    existing_disk = existing_disks[disk_name]
                    changes = []

                    if existing_disk.size != size_mb:
                        changes.append(
                            f"taille: {existing_disk.size/1024:.1f}GB -> {disk_info['size_gb']:.1f}GB"
                        )
                        existing_disk.size = size_mb

                    if existing_disk.description != description:
                        changes.append("description")
                        existing_disk.description = description

                    if changes:
                        existing_disk.save()
                        self.log_success(f"    Disque {disk_name} mis a jour ({', '.join(changes)})")
                    else:
                        self.log_info(f"    Disque {disk_name} deja a jour")

                else:
                    existing_check = VirtualDisk.objects.filter(
                        virtual_machine=netbox_vm,
                        name=disk_name
                    ).first()

                    if existing_check:
                        existing_check.size = size_mb
                        existing_check.description = description
                        existing_check.save()
                        self.log_success(f"    Disque {disk_name} mis a jour (double-check)")
                    else:
                        VirtualDisk.objects.create(
                            virtual_machine=netbox_vm,
                            name=disk_name,
                            size=size_mb,
                            description=description
                        )
                        self.log_success(f"    Disque {disk_name} cree ({disk_info['size_gb']:.1f}GB)")

            if self.cleanup_obsolete:
                for disk_name in set(existing_disks.keys()) - current_disk_names:
                    if any(disk_name.startswith(p) for p in proxmox_prefixes):
                        existing_disks[disk_name].delete()
                        self.log_warning(
                            f"    Disque {disk_name} supprime (n'existe plus dans Proxmox)"
                        )
                    else:
                        self.log_info(f"    Disque {disk_name} preserve (nom personnalise)")

        except Exception as e:
            self.log_failure(f"  Erreur synchronisation disques virtuels: {str(e)}")

    # -------------------------------------------------------------------------
    # Nettoyage VMs obsoletes
    # -------------------------------------------------------------------------

    def cleanup_obsolete_vms(self, cluster, proxmox_vm_names, commit):
        """Supprime les VMs qui n'existent plus dans Proxmox"""
        vminterface_ct = ContentType.objects.get_for_model(VMInterface)
        netbox_vms = VirtualMachine.objects.filter(cluster=cluster)

        if not commit:
            obsolete_count = 0
            for netbox_vm in netbox_vms:
                if netbox_vm.name not in proxmox_vm_names:
                    obsolete_count += 1
                    self.log_info(f"  [DRY-RUN] VM obsolete qui serait supprimee: {netbox_vm.name}")

            if obsolete_count > 0:
                self.log_warning(f"[DRY-RUN] {obsolete_count} VM(s) obsolete(s) detectee(s)")
            else:
                self.log_success(f"[DRY-RUN] Aucune VM obsolete detectee")
            return obsolete_count

        try:
            deleted_count = 0
            for netbox_vm in netbox_vms:
                if netbox_vm.name not in proxmox_vm_names:
                    self.log_warning(f"VM obsolete detectee: {netbox_vm.name}")

                    ip_count = 0
                    for interface in VMInterface.objects.filter(virtual_machine=netbox_vm):
                        for ip in IPAddress.objects.filter(
                            assigned_object_type=vminterface_ct,
                            assigned_object_id=interface.pk
                        ):
                            self.log_info(f"    Detachement IP: {ip.address}")
                            ip.assigned_object_type = None
                            ip.assigned_object_id = None
                            ip.status = 'deprecated'
                            ip.save()
                            ip_count += 1

                    netbox_vm.delete()
                    self.log_success(
                        f"    VM {netbox_vm.name} supprimee de NetBox "
                        f"({ip_count} IP(s) detachee(s))"
                    )
                    deleted_count += 1

            if deleted_count > 0:
                self.log_warning(f"Total VMs obsoletes supprimees: {deleted_count}")
            else:
                self.log_success(f"Aucune VM obsolete a supprimer")

            return deleted_count

        except Exception as e:
            self.log_failure(f"Erreur lors du nettoyage des VMs obsoletes: {str(e)}")
            return 0

    # -------------------------------------------------------------------------
    # Entrypoint
    # -------------------------------------------------------------------------

    def run(self, data, commit):
        self.log_info("=== Demarrage du script de synchronisation ===")
        urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

        self.cleanup_obsolete = data.get('cleanup_obsolete', True)

        headers = {
            'Authorization': f'PVEAPIToken={data["proxmox_token_id"]}={data["proxmox_token_secret"]}'
        }

        cluster = data['target_cluster']
        self.log_info(f"Utilisation du cluster : {cluster.name}")

        base_url = f'https://{data["proxmox_host"]}:8006/api2/json'
        nodes_data = self.proxmox_get(f'{base_url}/nodes', headers)

        if not nodes_data or 'data' not in nodes_data:
            return "Erreur lors de la recuperation des noeuds"

        all_vms = []
        for node in nodes_data['data']:
            node_name = node['node']
            self.log_info(f"\n=== Traitement du noeud : {node_name} ===")

            qemu_data = self.proxmox_get(f'{base_url}/nodes/{node_name}/qemu', headers)
            if qemu_data and 'data' in qemu_data:
                for vm in qemu_data['data']:
                    vm['node'] = node_name
                all_vms.extend(qemu_data['data'])

        if not all_vms:
            return "Aucune VM trouvee dans Proxmox"

        self.log_success(f"\n{len(all_vms)} VMs trouvees dans Proxmox:")
        for vm in all_vms[:10]:
            self.log_info(
                f"  - {vm.get('name', 'N/A')} "
                f"(ID: {vm.get('vmid')}, Node: {vm.get('node')}, Status: {vm.get('status')})"
            )
        if len(all_vms) > 10:
            self.log_info(f"  ... et {len(all_vms) - 10} autres VMs")

        vm_count = 0
        vm_created = 0
        vm_updated = 0
        interface_count = 0
        platform_count = 0
        connection_type_count = 0
        virtual_disk_count = 0
        vms_without_agent = []
        proxmox_vm_names = set()

        for vm in all_vms:
            vm_name = None
            try:
                vm_name = vm.get('name', f"vm-{vm.get('vmid')}")
                node_name = vm.get('node')
                vm_id = vm.get('vmid')

                proxmox_vm_names.add(vm_name)
                self.log_info(f"\n--- Traitement VM : {vm_name} (ID: {vm_id}) ---")

                platform = None
                if data.get('sync_platforms', True):
                    self.log_info(f"  Recuperation des informations OS...")
                    os_info = self.get_vm_os_info(base_url, node_name, vm_id, headers)
                    if os_info:
                        platform = self.create_or_get_platform(os_info, commit)
                        if platform:
                            platform_count += 1
                    else:
                        if vm.get('status') == 'running':
                            vms_without_agent.append(vm_name)

                vm_config = self.proxmox_get(
                    f'{base_url}/nodes/{node_name}/qemu/{vm_id}/config', headers
                )
                total_disk_gb = 0
                disk_details = []
                disk_details_str = "Aucune information disque"

                if vm_config and 'data' in vm_config:
                    total_disk_gb, disk_details = self.parse_proxmox_disk_config(vm_config['data'])
                    if disk_details:
                        disk_details_str = " | ".join(
                            [f"{d['key']}: {d['size_gb']:.1f}GB ({d['storage']})" for d in disk_details]
                        )
                    else:
                        total_disk_gb = int(vm.get('maxdisk', 0) / 1024 / 1024 / 1024)
                        disk_details_str = f"Total: {total_disk_gb}GB (via maxdisk)"
                else:
                    total_disk_gb = int(vm.get('maxdisk', 0) / 1024 / 1024 / 1024)
                    disk_details_str = f"Total: {total_disk_gb}GB (via maxdisk - config non disponible)"

                vm_data = {
                    'name': vm_name,
                    'cluster': cluster,
                    'status': 'active' if vm.get('status') == 'running' else 'offline',
                    'vcpus': vm.get('cpus', 1),
                    'memory': int(vm.get('maxmem', 0) / 1024 / 1024),
                    'disk': total_disk_gb,
                    'comments': (
                        f"Node: {vm.get('node', 'unknown')}\n"
                        f"VM ID: {vm.get('vmid')}\n"
                        f"CPU Usage: {vm.get('cpu', 0):.2%}\n"
                        f"Memory Usage: {int(vm.get('mem', 0) / 1024 / 1024)} MB"
                        f" / {int(vm.get('maxmem', 0) / 1024 / 1024)} MB\n"
                        f"Network IN: {int(vm.get('netin', 0) / 1024 / 1024)} MB\n"
                        f"Network OUT: {int(vm.get('netout', 0) / 1024 / 1024)} MB\n"
                        f"Uptime: {int(vm.get('uptime', 0) / 3600)} hours\n"
                        f"Disques: {disk_details_str}\n"
                        f"Derniere sync: {self.__class__.__name__}"
                    )
                }

                if platform:
                    vm_data['platform'] = platform

                if commit:
                    try:
                        with transaction.atomic():
                            netbox_vm, created = VirtualMachine.objects.update_or_create(
                                name=vm_name,
                                cluster=cluster,
                                defaults=vm_data
                            )

                            if created:
                                vm_created += 1
                                self.log_success(f"VM {vm_name} creee dans NetBox")
                            else:
                                vm_updated += 1
                                self.log_success(f"VM {vm_name} mise a jour dans NetBox")

                            vm_count += 1

                            if platform:
                                self.log_info(f"  Plateforme assignee: {platform.name}")

                            if data.get('sync_virtual_disks', True):
                                self.log_info(f"  Synchronisation des disques virtuels...")
                                self.sync_vm_virtual_disks(netbox_vm, disk_details, commit)
                                if disk_details:
                                    virtual_disk_count += 1

                            if data.get('sync_interfaces', True):
                                self.log_info(f"  Synchronisation des interfaces...")
                                vm_status = self.proxmox_get(
                                    f'{base_url}/nodes/{node_name}/qemu/{vm_id}/status/current',
                                    headers
                                )
                                self.sync_vm_interfaces(
                                    netbox_vm, vm_config, base_url, node_name,
                                    vm_id, headers, vm_status, commit
                                )
                                interface_count += 1

                            # FIX #1 : appel via apply_primary_ip au lieu de set_primary_ip
                            if data.get('set_primary_ip', True):
                                self.log_info(f"  Verification IP primaire...")
                                self.apply_primary_ip(netbox_vm, commit)

                            if data.get('sync_connection_type', True):
                                self.log_info(f"  Detection du type de connexion...")
                                self.set_connection_type(netbox_vm, commit)
                                connection_type_count += 1

                    except Exception as vm_sync_error:
                        self.log_failure(
                            f"  Erreur synchronisation VM {vm_name}: {str(vm_sync_error)}"
                        )
                        import traceback
                        self.log_debug(f"  Traceback: {traceback.format_exc()}")
                        continue

                else:
                    vm_count += 1
                    self.log_info(f"[DRY-RUN] VM {vm_name} serait traitee")
                    self.log_info(f"[DRY-RUN] Taille disque detectee: {total_disk_gb}GB")

                    if platform:
                        self.log_info(f"[DRY-RUN] Plateforme: {platform.name}")
                        platform_count += 1

                    if data.get('sync_virtual_disks', True) and disk_details:
                        self.sync_vm_virtual_disks(None, disk_details, commit)
                        virtual_disk_count += 1

                    if data.get('sync_interfaces', True):
                        vm_status = self.proxmox_get(
                            f'{base_url}/nodes/{node_name}/qemu/{vm_id}/status/current', headers
                        )
                        self.sync_vm_interfaces(
                            None, vm_config, base_url, node_name,
                            vm_id, headers, vm_status, commit
                        )
                        interface_count += 1

                    if data.get('sync_connection_type', True):
                        self.log_info(f"[DRY-RUN] Type de connexion serait analyse")
                        connection_type_count += 1

                    existing_vm = VirtualMachine.objects.filter(
                        name=vm_name, cluster=cluster
                    ).first()
                    if existing_vm:
                        vm_updated += 1
                        self.log_info(f"[DRY-RUN] VM existante qui serait mise a jour")
                    else:
                        vm_created += 1
                        self.log_info(f"[DRY-RUN] Nouvelle VM qui serait creee")

            except Exception as e:
                error_vm_name = vm_name if vm_name else vm.get('name', f"VM-{vm.get('vmid', 'unknown')}")
                self.log_failure(f"Erreur lors du traitement de la VM {error_vm_name}: {str(e)}")
                import traceback
                self.log_debug(f"Traceback complet: {traceback.format_exc()}")
                continue

        # Nettoyage des VMs obsoletes
        vm_deleted = 0
        if self.cleanup_obsolete:
            self.log_info(f"\n=== Nettoyage des VMs obsoletes ===")
            try:
                with transaction.atomic():
                    vm_deleted = self.cleanup_obsolete_vms(cluster, proxmox_vm_names, commit)
            except Exception as cleanup_error:
                self.log_failure(f"Erreur lors du nettoyage des VMs: {str(cleanup_error)}")
                import traceback
                self.log_debug(f"Traceback: {traceback.format_exc()}")

        # Resume final
        self.log_info("\n" + "=" * 60)
        self.log_info("=== RESUME DE LA SYNCHRONISATION ===")
        self.log_info("=" * 60)

        mode = "COMMIT" if commit else "DRY-RUN"
        result_msg = f"""
Mode: {mode}
{'=' * 60}

Statistiques de synchronisation:

VMs:
  Total traitees : {vm_count}
  Creees         : {vm_created}
  Mises a jour   : {vm_updated}
  Supprimees     : {vm_deleted}

Composants synchronises:"""

        if data.get('sync_virtual_disks', True):
            result_msg += f"\n  Disques virtuels   : {virtual_disk_count} VMs"
        if data.get('sync_interfaces', True):
            result_msg += f"\n  Interfaces reseau  : {interface_count} VMs"
        if data.get('sync_platforms', True):
            result_msg += f"\n  Plateformes OS     : {platform_count} detectees"
        if data.get('set_primary_ip', True):
            result_msg += f"\n  IPs primaires      : verifiees"
        if data.get('sync_connection_type', True):
            result_msg += f"\n  Types de connexion : {connection_type_count} analyses"

        if vms_without_agent:
            result_msg += f"\n\n  VMs sans agent QEMU ({len(vms_without_agent)}):"
            result_msg += "\n  " + ", ".join(vms_without_agent[:5])
            if len(vms_without_agent) > 5:
                result_msg += f"\n  ... et {len(vms_without_agent) - 5} autres"

        if self.cleanup_obsolete:
            result_msg += f"\n\n  Nettoyage : {'Actif' if commit else 'Mode simulation'}"
        else:
            result_msg += f"\n\n  Nettoyage : Desactive"

        if not commit:
            result_msg += (
                f"\n\n  MODE DRY-RUN : Aucune modification n'a ete effectuee."
                f"\n    Relancez avec 'Commit changes' pour appliquer les modifications."
            )

        self.log_info(result_msg)
        self.log_info("=" * 60)

        return result_msg
