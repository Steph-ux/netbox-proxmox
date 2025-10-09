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
        description = "Synchronise les VMs Proxmox vers NetBox avec nettoyage automatique des éléments obsolètes"
        commit_default = True
        field_order = ['target_cluster', 'proxmox_host', 'proxmox_token_id', 'proxmox_token_secret', 
                      'sync_interfaces', 'sync_platforms', 'set_primary_ip', 'sync_connection_type', 
                      'sync_virtual_disks', 'cleanup_obsolete']
        
    target_cluster = ObjectVar(
        description="Sélectionnez le cluster NetBox où ajouter les VMs",
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
        description="Synchroniser les interfaces réseau et adresses IP",
        label="Synchroniser les interfaces",
        default=True
    )

    sync_platforms = BooleanVar(
        description="Synchroniser les informations de plateforme OS",
        label="Synchroniser les plateformes",
        default=True
    )
    
    set_primary_ip = BooleanVar(
        description="Définir automatiquement la première IP comme IP primaire",
        label="Définir IP primaire",
        default=True
    )

    sync_connection_type = BooleanVar(
        description="Détecter automatiquement le type de connexion (Private/Public) basé sur les IPs",
        label="Synchroniser type connexion",
        default=True
    )

    sync_virtual_disks = BooleanVar(
        description="Synchroniser les disques virtuels comme objets NetBox séparés",
        label="Synchroniser les disques virtuels",
        default=True
    )

    cleanup_obsolete = BooleanVar(
        description="ATTENTION: Supprimer automatiquement les VMs, interfaces et IPs qui n'existent plus dans Proxmox",
        label="Nettoyer les éléments obsolètes",
        default=True
    )

    def proxmox_get(self, url, headers):
        """Effectue une requête GET vers l'API Proxmox avec logs détaillés"""
        try:
            self.log_info(f"Requête API : {url}")
            response = requests.get(url, headers=headers, verify=False, timeout=30)
            self.log_info(f"Code réponse : {response.status_code}")
            
            if response.status_code == 200:
                return response.json()
            else:
                self.log_warning(f"Réponse non-200 : {response.status_code} - {response.text}")
            return None
        except Exception as e:
            self.log_failure(f"Erreur lors de la requête {url}: {str(e)}")
            return None

    def parse_mac_address(self, mac_str):
        """Parse et formate une adresse MAC de manière normalisée"""
        if not mac_str:
            return None
        mac_clean = re.sub(r'[^a-fA-F0-9]', '', mac_str.lower())
        if len(mac_clean) == 12:
            return ':'.join(mac_clean[i:i+2] for i in range(0, 12, 2)).upper()
        return None

    def parse_proxmox_network_config(self, config_data):
        """Parse la configuration réseau Proxmox"""
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
        """Parse la configuration des disques Proxmox pour obtenir la taille réelle"""
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
                            self.log_info(f"      Disque trouvé: {key} = {disk_size_gb:.1f}GB ({disk_info['storage']})")
                        
                    except Exception as e:
                        self.log_warning(f"      Erreur parsing disque {key}: {str(e)}")
        
        return int(total_disk_gb), disk_details

    def get_vm_os_info(self, base_url, node_name, vm_id, headers):
        """Récupère les informations OS via l'agent QEMU"""
        try:
            os_info_data = self.proxmox_get(f'{base_url}/nodes/{node_name}/qemu/{vm_id}/agent/get-osinfo', headers)
            if os_info_data and 'data' in os_info_data and 'result' in os_info_data['data']:
                return os_info_data['data']['result']
            return None
        except Exception as e:
            self.log_warning(f"      Agent OS info non disponible: {str(e)}")
            return None

    def get_vm_network_interfaces(self, base_url, node_name, vm_id, headers):
        """Récupère les interfaces réseau via l'agent QEMU"""
        try:
            network_data = self.proxmox_get(f'{base_url}/nodes/{node_name}/qemu/{vm_id}/agent/network-get-interfaces', headers)
            if network_data and 'data' in network_data and 'result' in network_data['data']:
                return network_data['data']['result']
            return None
        except Exception as e:
            self.log_warning(f"      Agent network info non disponible: {str(e)}")
            return None

    def get_vm_network_status_fallback(self, vm_status_data):
        """Récupère les adresses IP depuis le status de la VM (méthode fallback)"""
        ip_addresses = []
        
        if 'agent-netinfo' in vm_status_data:
            netinfo = vm_status_data['agent-netinfo']
            if 'result' in netinfo:
                for interface in netinfo['result']:
                    if 'ip-addresses' in interface:
                        for ip_info in interface['ip-addresses']:
                            ip_addr = ip_info.get('ip-address')
                            prefix = ip_info.get('prefix', 24)
                            ip_type = ip_info.get('ip-address-type', 'ipv4')
                            
                            if ip_addr and ip_type == 'ipv4' and not ip_addr.startswith('127.'):
                                ip_addresses.append({
                                    'address': f"{ip_addr}/{prefix}",
                                    'interface': interface.get('name', 'unknown'),
                                    'mac_address': self.parse_mac_address(interface.get('hardware-address', ''))
                                })
        
        return ip_addresses

    def create_or_get_platform(self, os_info, commit):
        """Crée ou récupère une plateforme basée sur les infos OS"""
        if not os_info or not commit:
            return None
        
        try:
            platform_name = os_info.get('pretty-name') or os_info.get('name', 'Unknown OS')
            
            # Chercher d'abord si elle existe
            platform = Platform.objects.filter(name=platform_name).first()
            
            if not platform:
                platform_slug = re.sub(r'[^a-zA-Z0-9\-_]', '-', platform_name.lower())[:50]
                
                # Assurer l'unicité du slug
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
                self.log_success(f"    Plateforme créée: {platform_name}")
            else:
                self.log_info(f"    Plateforme existante: {platform_name}")
            
            return platform
            
        except Exception as e:
            self.log_failure(f"    Erreur création plateforme {platform_name if 'platform_name' in locals() else 'unknown'}: {str(e)}")
            import traceback
            self.log_debug(f"    Traceback: {traceback.format_exc()}")
            return None

    def find_interface_by_mac(self, netbox_vm, mac_address):
        """Trouve une interface par son adresse MAC normalisée"""
        if not mac_address:
            return None
            
        try:
            from dcim.models import MACAddress
            
            # Normaliser la MAC pour la recherche
            normalized_mac = self.parse_mac_address(mac_address)
            if not normalized_mac:
                return None
            
            # Chercher l'objet MACAddress
            mac_obj = MACAddress.objects.filter(mac_address=normalized_mac).first()
            if mac_obj:
                if (mac_obj.assigned_object_type and 
                    hasattr(mac_obj.assigned_object, 'virtual_machine') and
                    mac_obj.assigned_object.virtual_machine == netbox_vm):
                    return mac_obj.assigned_object
            
            # Fallback: chercher dans toutes les interfaces de la VM
            for interface in VMInterface.objects.filter(virtual_machine=netbox_vm):
                interface_mac_obj = MACAddress.objects.filter(
                    assigned_object_type=ContentType.objects.get_for_model(VMInterface),
                    assigned_object_id=interface.pk
                ).first()
                
                if interface_mac_obj:
                    # Convertir EUI object en string pour comparaison
                    mac_str = str(interface_mac_obj.mac_address).upper().replace('-', ':')
                    if mac_str == normalized_mac.upper():
                        return interface
            
            return None
        except Exception as e:
            self.log_warning(f"    Erreur recherche interface par MAC {mac_address}: {str(e)}")
            return None

    def sync_vm_interfaces(self, netbox_vm, vm_config, base_url, node_name, vm_id, headers, vm_status, commit):
        """Synchronise les interfaces d'une VM avec nettoyage des interfaces obsolètes"""
        if not vm_config or 'data' not in vm_config:
            return
        
        config_data = vm_config['data']
        interfaces_config = self.parse_proxmox_network_config(config_data)
        
        agent_interfaces = self.get_vm_network_interfaces(base_url, node_name, vm_id, headers)
        
        ip_addresses = []
        agent_available = False
        
        if agent_interfaces:
            agent_available = True
            self.log_success(f"  Agent QEMU disponible - utilisation des données agent")
            
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
            self.log_warning(f"  Agent QEMU non disponible pour VM {netbox_vm.name if netbox_vm else vm_id} - utilisation du fallback")
            status_data = vm_status.get('data', {}) if vm_status else {}
            ip_addresses = self.get_vm_network_status_fallback(status_data)
        
        if not commit:
            self.log_info(f"  [DRY-RUN] Interfaces trouvées: {len(interfaces_config)}")
            self.log_info(f"  [DRY-RUN] IPs trouvées: {len(ip_addresses)}")
            self.log_info(f"  [DRY-RUN] Agent QEMU: {'Disponible' if agent_available else 'Non disponible'}")
            return
        
        # Track des interfaces synchronisées (par MAC normalisée)
        synced_interface_macs = set()
        synced_interface_names = set()
        
        # Synchronise chaque interface
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
                
                if agent_available:
                    desc_parts.append("Source: QEMU Agent")
                else:
                    desc_parts.append("Source: Proxmox Config (Agent non disponible)")
                
                new_description = " | ".join(desc_parts) if desc_parts else ""
                
                if existing_interface:
                    self.log_info(f"    Interface existante trouvée: {existing_interface.name} (MAC: {mac_address})")
                    
                    # Préserver les noms personnalisés (qui ne commencent pas par 'net')
                    if existing_interface.name.startswith('net'):
                        interface_data['name'] = interface_config['name']
                    else:
                        interface_data['name'] = existing_interface.name
                        self.log_info(f"      Préservation du nom personnalisé: {existing_interface.name}")
                    
                    # Préserver les descriptions personnalisées
                    if existing_interface.description:
                        if not any(keyword in existing_interface.description.lower() 
                                 for keyword in ['model:', 'bridge:', 'vlan:', 'source:']):
                            if new_description:
                                interface_data['description'] = f"{existing_interface.description} | {new_description}"
                            else:
                                interface_data['description'] = existing_interface.description
                        else:
                            interface_data['description'] = new_description
                    else:
                        interface_data['description'] = new_description
                    
                    for key, value in interface_data.items():
                        if value is not None:
                            setattr(existing_interface, key, value)
                    existing_interface.save()
                    
                    netbox_interface = existing_interface
                    self.log_success(f"    Interface {netbox_interface.name} mise à jour")
                    
                else:
                    interface_data['name'] = interface_config['name']
                    interface_data['description'] = new_description
                    
                    # Vérifier si une interface avec ce nom existe déjà
                    existing_by_name = VMInterface.objects.filter(
                        virtual_machine=netbox_vm,
                        name=interface_config['name']
                    ).first()
                    
                    if existing_by_name:
                        self.log_warning(f"    Interface {interface_config['name']} existe déjà, mise à jour...")
                        
                        for key, value in interface_data.items():
                            if key != 'name' and value is not None:
                                setattr(existing_by_name, key, value)
                        existing_by_name.save()
                        
                        netbox_interface = existing_by_name
                        self.log_success(f"    Interface {interface_config['name']} mise à jour")
                    else:
                        netbox_interface = VMInterface.objects.create(**interface_data)
                        self.log_success(f"    Interface {interface_data['name']} créée")
                
                # Assigner la MAC à l'interface
                if mac_address:
                    self.assign_mac_to_interface(netbox_interface, mac_address)
                
                # Synchroniser les IPs
                self.sync_interface_ips(netbox_interface, ip_addresses, mac_address, agent_available)
                
            except Exception as e:
                self.log_failure(f"    Erreur interface {interface_config.get('name', 'unknown')}: {str(e)}")
        
                        # NETTOYAGE: Supprimer les interfaces obsolètes
        if self.cleanup_obsolete:
            try:
                all_vm_interfaces = VMInterface.objects.filter(virtual_machine=netbox_vm)
                for interface in all_vm_interfaces:
                    should_delete = False
                    
                    # Vérifier si l'interface est toujours dans Proxmox
                    if interface.name.startswith('net'):
                        # Interface Proxmox standard
                        if interface.name not in synced_interface_names:
                            should_delete = True
                    else:
                        # Interface avec nom personnalisé - vérifier par MAC
                        try:
                            from dcim.models import MACAddress
                            mac_obj = MACAddress.objects.filter(
                                assigned_object_type=ContentType.objects.get_for_model(VMInterface),
                                assigned_object_id=interface.pk
                            ).first()
                            
                            if mac_obj:
                                # Convertir EUI object en string
                                mac_str = str(mac_obj.mac_address).upper().replace('-', ':')
                                if mac_str not in synced_interface_macs:
                                    should_delete = True
                        except Exception as mac_error:
                            self.log_warning(f"    Erreur vérification MAC pour {interface.name}: {str(mac_error)}")
                    
                    if should_delete:
                        self.log_warning(f"    🗑️  Suppression interface obsolète: {interface.name}")
                        interface.delete()
            except Exception as cleanup_error:
                self.log_warning(f"    Erreur nettoyage interfaces: {str(cleanup_error)}")

    def assign_mac_to_interface(self, netbox_interface, mac_address):
        """Assigne une adresse MAC à une interface en utilisant le modèle MACAddress"""
        try:
            from dcim.models import MACAddress
            
            # Normaliser la MAC
            normalized_mac = self.parse_mac_address(mac_address)
            if not normalized_mac:
                return
            
            mac_obj = MACAddress.objects.filter(mac_address=normalized_mac).first()
            
            interface_content_type = ContentType.objects.get_for_model(VMInterface)
            
            if mac_obj:
                if (mac_obj.assigned_object_type == interface_content_type and 
                    mac_obj.assigned_object_id == netbox_interface.pk):
                    self.log_info(f"      MAC {normalized_mac} déjà assignée à cette interface")
                    return
                elif mac_obj.assigned_object:
                    self.log_warning(f"      MAC {normalized_mac} réassignée à l'interface {netbox_interface.name}")
                
                mac_obj.assigned_object_type = interface_content_type
                mac_obj.assigned_object_id = netbox_interface.pk
                mac_obj.save()
            else:
                MACAddress.objects.create(
                    mac_address=normalized_mac,
                    assigned_object_type=interface_content_type,
                    assigned_object_id=netbox_interface.pk
                )
                self.log_success(f"      MAC {normalized_mac} créée et assignée")
                
        except Exception as e:
            self.log_warning(f"      Erreur assignation MAC {mac_address}: {str(e)}")

    def sync_interface_ips(self, netbox_interface, ip_addresses, interface_mac, agent_available):
        """Synchronise les adresses IP d'une interface avec nettoyage des IPs obsolètes"""
        
        interface_ips = []
        
        if agent_available and interface_mac:
            # Normaliser la MAC pour la comparaison
            normalized_interface_mac = self.parse_mac_address(interface_mac)
            interface_ips = [
                ip for ip in ip_addresses 
                if ip.get('mac_address') and 
                self.parse_mac_address(ip.get('mac_address')) == normalized_interface_mac
            ]
        else:
            proxmox_interface_name = netbox_interface.name if netbox_interface.name.startswith('net') else None
            if proxmox_interface_name:
                interface_ips = [ip for ip in ip_addresses if ip['interface'] == proxmox_interface_name]
        
        if not interface_ips:
            self.log_info(f"      Aucune IP trouvée pour l'interface {netbox_interface.name}")
            
            # NETTOYAGE: Détacher les IPs qui ne sont plus présentes
            if self.cleanup_obsolete:
                existing_ips = IPAddress.objects.filter(
                    assigned_object_type=ContentType.objects.get_for_model(VMInterface),
                    assigned_object_id=netbox_interface.pk
                )
                for ip in existing_ips:
                    self.log_warning(f"      🗑️  Détachement IP obsolète: {ip.address}")
                    ip.assigned_object_type = None
                    ip.assigned_object_id = None
                    ip.status = 'deprecated'
                    ip.save()
            return
        
        # Track des IPs synchronisées (normaliser les adresses)
        synced_ip_addresses = set()
        for ip_info in interface_ips:
            try:
                # Normaliser l'adresse IP
                ip_address = str(ipaddress.ip_interface(ip_info['address']))
                synced_ip_addresses.add(ip_address)
            except ValueError:
                self.log_warning(f"      IP invalide ignorée: {ip_info['address']}")
                continue
        
        for ip_info in interface_ips:
            try:
                # Normaliser l'adresse IP
                ip_address = str(ipaddress.ip_interface(ip_info['address']))
                
                existing_ip = IPAddress.objects.filter(address=ip_address).first()
                
                if existing_ip:
                    if (existing_ip.assigned_object_type and 
                        existing_ip.assigned_object_id == netbox_interface.pk):
                        existing_ip.status = 'active'
                        existing_ip.save()
                        self.log_info(f"      IP {ip_address} déjà assignée à cette interface")
                        
                    elif existing_ip.assigned_object:
                        if hasattr(existing_ip.assigned_object, 'virtual_machine'):
                            other_vm = existing_ip.assigned_object.virtual_machine
                            if other_vm == netbox_interface.virtual_machine:
                                content_type = ContentType.objects.get_for_model(VMInterface)
                                existing_ip.assigned_object_type = content_type
                                existing_ip.assigned_object_id = netbox_interface.pk
                                existing_ip.status = 'active'
                                existing_ip.save()
                                self.log_warning(f"      IP {ip_address} réassignée de {existing_ip.assigned_object.name} vers {netbox_interface.name}")
                            else:
                                self.log_warning(f"      IP {ip_address} déjà assignée à une autre VM ({other_vm.name}), ignorée")
                                continue
                        else:
                            self.log_warning(f"      IP {ip_address} déjà assignée à un équipement physique, ignorée")
                            continue
                    else:
                        content_type = ContentType.objects.get_for_model(VMInterface)
                        existing_ip.assigned_object_type = content_type
                        existing_ip.assigned_object_id = netbox_interface.pk
                        existing_ip.status = 'active'
                        existing_ip.save()
                        self.log_success(f"      IP {ip_address} assignée à l'interface")
                
                else:
                    content_type = ContentType.objects.get_for_model(VMInterface)
                    
                    ip_data = {
                        'address': ip_address,
                        'assigned_object_type': content_type,
                        'assigned_object_id': netbox_interface.pk,
                        'status': 'active'
                    }
                    
                    netbox_ip = IPAddress.objects.create(**ip_data)
                    self.log_success(f"      IP {ip_address} créée et assignée")
                
            except Exception as e:
                self.log_failure(f"      Erreur IP {ip_info.get('address', 'unknown')}: {str(e)}")
        
        # NETTOYAGE: Détacher les IPs qui ne sont plus présentes
        if self.cleanup_obsolete:
            existing_ips = IPAddress.objects.filter(
                assigned_object_type=ContentType.objects.get_for_model(VMInterface),
                assigned_object_id=netbox_interface.pk
            )
            for ip in existing_ips:
                # Normaliser l'adresse pour la comparaison
                try:
                    normalized_existing = str(ipaddress.ip_interface(str(ip.address)))
                    if normalized_existing not in synced_ip_addresses:
                        self.log_warning(f"      🗑️  Détachement IP obsolète: {ip.address}")
                        ip.assigned_object_type = None
                        ip.assigned_object_id = None
                        ip.status = 'deprecated'
                        ip.save()
                except ValueError:
                    self.log_warning(f"      IP invalide détectée: {ip.address}")

    def set_primary_ip(self, netbox_vm, commit):
        """Définit la première IP comme IP primaire si aucune n'est définie"""
        
        if not commit:
            self.log_info(f"  [DRY-RUN] Vérification IP primaire pour {netbox_vm.name if netbox_vm else 'VM'}")
            return
        
        if netbox_vm.primary_ip4:
            self.log_info(f"  IP primaire déjà définie: {netbox_vm.primary_ip4.address}")
            return
        
        try:
            interfaces = VMInterface.objects.filter(
                virtual_machine=netbox_vm
            ).order_by('name')
            
            for interface in interfaces:
                interface_ips = IPAddress.objects.filter(
                    assigned_object_type=ContentType.objects.get_for_model(VMInterface),
                    assigned_object_id=interface.pk,
                    status='active'
                ).order_by('address')
                
                if interface_ips.exists():
                    first_ip = interface_ips.first()
                    netbox_vm.primary_ip4 = first_ip
                    netbox_vm.save()
                    self.log_success(f"  IP primaire définie: {first_ip.address} (interface: {interface.name})")
                    return
            
            self.log_info(f"  Aucune IP active trouvée pour définir comme primaire")
            
        except Exception as e:
            self.log_warning(f"  Erreur lors de la définition de l'IP primaire: {str(e)}")

    def is_private_ip(self, ip_address):
        """Détermine si une adresse IP est privée"""
        try:
            ip_str = ip_address.split('/')[0]
            ip_obj = ipaddress.ip_address(ip_str)
            return ip_obj.is_private
        except ValueError:
            return True
    
    def determine_connection_type(self, vm_interfaces):
        """Détermine le type de connexion basé sur les IPs de la VM"""
        has_public = False
        has_private = False
        
        try:
            for interface in vm_interfaces:
                interface_ips = IPAddress.objects.filter(
                    assigned_object_type=ContentType.objects.get_for_model(VMInterface),
                    assigned_object_id=interface.pk,
                    status='active'
                )
                
                for ip_obj in interface_ips:
                    ip_address = str(ip_obj.address)
                    
                    if ip_address.startswith('127.'):
                        continue
                    
                    if self.is_private_ip(ip_address):
                        has_private = True
                        self.log_info(f"      IP privée détectée: {ip_address}")
                    else:
                        has_public = True
                        self.log_info(f"      IP publique détectée: {ip_address}")
            
            if has_public:
                return "Public"
            elif has_private:
                return "Private"
            else:
                return None
                
        except Exception as e:
            self.log_warning(f"      Erreur détection type connexion: {str(e)}")
            return None
    
    def set_connection_type(self, netbox_vm, commit):
        """Définit le champ Server_Connection_Type basé sur les IPs"""
        
        if not commit:
            self.log_info(f"  [DRY-RUN] Détection type de connexion pour {netbox_vm.name if netbox_vm else 'VM'}")
            return
        
        try:
            vm_interfaces = VMInterface.objects.filter(virtual_machine=netbox_vm)
            
            if not vm_interfaces.exists():
                self.log_info(f"  Aucune interface trouvée pour déterminer le type de connexion")
                return
            
            connection_type = self.determine_connection_type(vm_interfaces)
            
            if connection_type:
                current_type = netbox_vm.custom_field_data.get('Server_Connection_Type')
                
                if current_type != connection_type:
                    if not netbox_vm.custom_field_data:
                        netbox_vm.custom_field_data = {}
                    netbox_vm.custom_field_data['Server_Connection_Type'] = connection_type
                    netbox_vm.save()
                    self.log_success(f"  Type de connexion défini: {connection_type}")
                else:
                    self.log_info(f"  Type de connexion déjà correct: {connection_type}")
            else:
                self.log_info(f"  Impossible de déterminer le type de connexion (aucune IP valide)")
                
        except Exception as e:
            self.log_warning(f"  Erreur lors de la définition du type de connexion: {str(e)}")

    def sync_vm_virtual_disks(self, netbox_vm, disk_details, commit):
        """Synchronise les disques virtuels d'une VM avec nettoyage des disques obsolètes"""
        
        if not disk_details:
            if commit:
                # Si aucun disque dans Proxmox, supprimer les disques Proxmox dans NetBox
                if self.cleanup_obsolete:
                    existing_disks = VirtualDisk.objects.filter(virtual_machine=netbox_vm)
                    for disk in existing_disks:
                        # Ne supprimer que les disques Proxmox standard
                        if any(disk.name.startswith(prefix) for prefix in ['scsi', 'ide', 'sata', 'virtio', 'efidisk', 'tpmstate']):
                            disk.delete()
                            self.log_warning(f"    🗑️  Disque {disk.name} supprimé (n'existe plus dans Proxmox)")
            return
        
        if not commit:
            self.log_info(f"  [DRY-RUN] {len(disk_details)} disques seraient synchronisés")
            for disk_info in disk_details:
                self.log_info(f"    - {disk_info['key']}: {disk_info['size_gb']:.1f}GB ({disk_info['storage']})")
            return
        
        try:
            existing_disks = {disk.name: disk for disk in VirtualDisk.objects.filter(virtual_machine=netbox_vm)}
            current_disk_names = set()
            
            for disk_info in disk_details:
                disk_name = disk_info['key']
                current_disk_names.add(disk_name)
                
                disk_data = {
                    'virtual_machine': netbox_vm,
                    'name': disk_name,
                    'size': int(disk_info['size_gb'] * 1024),  # Convertir GB en MB
                    'description': f"Storage: {disk_info['storage']} | Type: {disk_info['type']} | Size: {disk_info['size_gb']:.1f}GB"
                }
                
                if disk_name in existing_disks:
                    existing_disk = existing_disks[disk_name]
                    
                    changes = []
                    if existing_disk.size != disk_data['size']:
                        changes.append(f"taille: {existing_disk.size/1024:.1f}GB → {disk_info['size_gb']:.1f}GB")
                        existing_disk.size = disk_data['size']
                    
                    if existing_disk.description != disk_data['description']:
                        changes.append("description")
                        existing_disk.description = disk_data['description']
                    
                    if changes:
                        existing_disk.save()
                        self.log_success(f"    Disque {disk_name} mis à jour ({', '.join(changes)})")
                    else:
                        self.log_info(f"    Disque {disk_name} déjà à jour")
                
                else:
                    # Vérifier qu'il n'existe pas déjà (au cas où)
                    existing_check = VirtualDisk.objects.filter(
                        virtual_machine=netbox_vm,
                        name=disk_name
                    ).first()
                    
                    if existing_check:
                        # Mettre à jour au lieu de créer
                        existing_check.size = disk_data['size']
                        existing_check.description = disk_data['description']
                        existing_check.save()
                        self.log_success(f"    Disque {disk_name} mis à jour (trouvé par double-check)")
                    else:
                        new_disk = VirtualDisk.objects.create(**disk_data)
                        self.log_success(f"    Disque {disk_name} créé ({disk_info['size_gb']:.1f}GB)")
            
            # NETTOYAGE: Supprimer les disques qui n'existent plus dans Proxmox
            if self.cleanup_obsolete:
                disks_to_remove = set(existing_disks.keys()) - current_disk_names
                for disk_name in disks_to_remove:
                    disk_to_delete = existing_disks[disk_name]
                    # Ne supprimer que les disques Proxmox standard
                    if any(disk_name.startswith(prefix) for prefix in ['scsi', 'ide', 'sata', 'virtio', 'efidisk', 'tpmstate']):
                        disk_to_delete.delete()
                        self.log_warning(f"    🗑️  Disque {disk_name} supprimé (n'existe plus dans Proxmox)")
                    else:
                        self.log_info(f"    Disque {disk_name} préservé (nom personnalisé)")
                    
        except Exception as e:
            self.log_failure(f"  Erreur synchronisation disques virtuels: {str(e)}")

    def cleanup_obsolete_vms(self, cluster, proxmox_vm_names, commit):
        """Supprime les VMs qui n'existent plus dans Proxmox"""
        
        if not commit:
            netbox_vms = VirtualMachine.objects.filter(cluster=cluster)
            obsolete_count = 0
            for netbox_vm in netbox_vms:
                if netbox_vm.name not in proxmox_vm_names:
                    obsolete_count += 1
                    self.log_info(f"  [DRY-RUN] VM obsolète qui serait supprimée: {netbox_vm.name}")
            
            if obsolete_count > 0:
                self.log_warning(f"[DRY-RUN] {obsolete_count} VM(s) obsolète(s) détectée(s)")
            else:
                self.log_success(f"[DRY-RUN] Aucune VM obsolète détectée")
            return obsolete_count
        
        try:
            # Récupérer toutes les VMs NetBox du cluster
            netbox_vms = VirtualMachine.objects.filter(cluster=cluster)
            
            deleted_count = 0
            for netbox_vm in netbox_vms:
                if netbox_vm.name not in proxmox_vm_names:
                    self.log_warning(f"🗑️  VM obsolète détectée: {netbox_vm.name}")
                    
                    # Détacher les IPs avant suppression
                    interfaces = VMInterface.objects.filter(virtual_machine=netbox_vm)
                    ip_count = 0
                    for interface in interfaces:
                        ips = IPAddress.objects.filter(
                            assigned_object_type=ContentType.objects.get_for_model(VMInterface),
                            assigned_object_id=interface.pk
                        )
                        for ip in ips:
                            self.log_info(f"    Détachement IP: {ip.address}")
                            ip.assigned_object_type = None
                            ip.assigned_object_id = None
                            ip.status = 'deprecated'
                            ip.save()
                            ip_count += 1
                    
                    # Supprimer la VM (cascade supprimera interfaces et disques)
                    netbox_vm.delete()
                    self.log_success(f"    VM {netbox_vm.name} supprimée de NetBox ({ip_count} IP(s) détachée(s))")
                    deleted_count += 1
            
            if deleted_count > 0:
                self.log_warning(f"Total VMs obsolètes supprimées: {deleted_count}")
            else:
                self.log_success(f"Aucune VM obsolète à supprimer")
            
            return deleted_count
            
        except Exception as e:
            self.log_failure(f"Erreur lors du nettoyage des VMs obsolètes: {str(e)}")
            return 0

    def run(self, data, commit):
        self.log_info("=== Démarrage du script de synchronisation ===")
        urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
        
        # Stocker cleanup_obsolete comme attribut d'instance
        self.cleanup_obsolete = data.get('cleanup_obsolete', True)
        
        headers = {
            'Authorization': f'PVEAPIToken={data["proxmox_token_id"]}={data["proxmox_token_secret"]}'
        }

        cluster = data['target_cluster']
        self.log_info(f"Utilisation du cluster : {cluster.name}")

        base_url = f'https://{data["proxmox_host"]}:8006/api2/json'
        nodes_data = self.proxmox_get(f'{base_url}/nodes', headers)
        
        if not nodes_data or 'data' not in nodes_data:
            return "Erreur lors de la récupération des nœuds"

        # Récupération de toutes les VMs Proxmox
        all_vms = []
        for node in nodes_data['data']:
            node_name = node['node']
            self.log_info(f"\n=== Traitement du nœud : {node_name} ===")

            qemu_data = self.proxmox_get(f'{base_url}/nodes/{node_name}/qemu', headers)
            if qemu_data and 'data' in qemu_data:
                for vm in qemu_data['data']:
                    vm['node'] = node_name
                all_vms.extend(qemu_data['data'])

        if not all_vms:
            return "Aucune VM trouvée dans Proxmox"

        # Log des VMs trouvées
        self.log_success(f"\n✅ {len(all_vms)} VMs trouvées dans Proxmox:")
        for vm in all_vms[:10]:  # Afficher les 10 premières
            self.log_info(f"  - {vm.get('name', 'N/A')} (ID: {vm.get('vmid')}, Node: {vm.get('node')}, Status: {vm.get('status')})")
        if len(all_vms) > 10:
            self.log_info(f"  ... et {len(all_vms) - 10} autres VMs")

        # Statistiques
        vm_count = 0
        vm_created = 0
        vm_updated = 0
        interface_count = 0
        platform_count = 0
        connection_type_count = 0
        virtual_disk_count = 0
        vms_without_agent = []
        
        # Track des VMs trouvées dans Proxmox
        proxmox_vm_names = set()
        
        # Traitement des VMs
        for vm in all_vms:
            vm_name = None
            try:
                vm_name = vm.get('name', f"vm-{vm.get('vmid')}")
                node_name = vm.get('node')
                vm_id = vm.get('vmid')
                
                proxmox_vm_names.add(vm_name)
                
                self.log_info(f"\n--- Traitement VM : {vm_name} (ID: {vm_id}) ---")
                
                # Synchronisation de la plateforme
                platform = None
                if data.get('sync_platforms', True):
                    self.log_info(f"  Récupération des informations OS...")
                    os_info = self.get_vm_os_info(base_url, node_name, vm_id, headers)
                    if os_info:
                        platform = self.create_or_get_platform(os_info, commit)
                        if platform:
                            platform_count += 1
                    else:
                        if vm.get('status') == 'running':
                            vms_without_agent.append(vm_name)
                
                # Récupération de la configuration de la VM
                vm_config = self.proxmox_get(f'{base_url}/nodes/{node_name}/qemu/{vm_id}/config', headers)
                total_disk_gb = 0
                disk_details = []
                disk_details_str = "Aucune information disque"
                
                if vm_config and 'data' in vm_config:
                    total_disk_gb, disk_details = self.parse_proxmox_disk_config(vm_config['data'])
                    if disk_details:
                        disk_details_str = " | ".join([f"{d['key']}: {d['size_gb']:.1f}GB ({d['storage']})" for d in disk_details])
                    else:
                        total_disk_gb = int(vm.get('maxdisk', 0) / 1024 / 1024 / 1024)
                        disk_details_str = f"Total: {total_disk_gb}GB (via maxdisk)"
                else:
                    total_disk_gb = int(vm.get('maxdisk', 0) / 1024 / 1024 / 1024)
                    disk_details_str = f"Total: {total_disk_gb}GB (via maxdisk - config non disponible)"

                # Préparation des données de la VM
                vm_data = {
                    'name': vm_name,
                    'cluster': cluster,
                    'status': 'active' if vm.get('status') == 'running' else 'offline',
                    'vcpus': vm.get('cpus', 1),
                    'memory': int(vm.get('maxmem', 0) / 1024 / 1024),
                    'disk': total_disk_gb,
                    'comments': f"""Node: {vm.get('node', 'unknown')}
VM ID: {vm.get('vmid')}
CPU Usage: {vm.get('cpu', 0):.2%}
Memory Usage: {int(vm.get('mem', 0) / 1024 / 1024)} MB / {int(vm.get('maxmem', 0) / 1024 / 1024)} MB
Network IN: {int(vm.get('netin', 0) / 1024 / 1024)} MB
Network OUT: {int(vm.get('netout', 0) / 1024 / 1024)} MB
Uptime: {int(vm.get('uptime', 0) / 3600)} hours
Disques: {disk_details_str}
Dernière sync: {self.__class__.__name__}"""
                }

                if platform:
                    vm_data['platform'] = platform

                if commit:
                    # Utiliser une sous-transaction pour isoler les erreurs
                    try:
                        with transaction.atomic():
                            # Créer ou mettre à jour la VM dans NetBox
                            netbox_vm, created = VirtualMachine.objects.update_or_create(
                                name=vm_name,
                                cluster=cluster,
                                defaults=vm_data
                            )
                            
                            if created:
                                vm_created += 1
                                self.log_success(f"✨ VM {vm_name} créée dans NetBox")
                            else:
                                vm_updated += 1
                                self.log_success(f"🔄 VM {vm_name} mise à jour dans NetBox")
                            
                            vm_count += 1  # FIX: Incrémenter le compteur en mode commit
                            
                            if platform:
                                self.log_info(f"  Plateforme assignée: {platform.name}")
                            
                            # Synchronisation des disques virtuels
                            if data.get('sync_virtual_disks', True):
                                self.log_info(f"  Synchronisation des disques virtuels...")
                                self.sync_vm_virtual_disks(netbox_vm, disk_details, commit)
                                if disk_details:
                                    virtual_disk_count += 1
                            
                            # Synchronisation des interfaces
                            if data.get('sync_interfaces', True):
                                self.log_info(f"  Synchronisation des interfaces...")
                                vm_status = self.proxmox_get(f'{base_url}/nodes/{node_name}/qemu/{vm_id}/status/current', headers)
                                self.sync_vm_interfaces(netbox_vm, vm_config, base_url, node_name, vm_id, headers, vm_status, commit)
                                interface_count += 1
                            
                            # Définition de l'IP primaire
                            if data.get('set_primary_ip', True):
                                self.log_info(f"  Vérification IP primaire...")
                                self.set_primary_ip(netbox_vm, commit)
                            
                            # Détection du type de connexion
                            if data.get('sync_connection_type', True):
                                self.log_info(f"  Détection du type de connexion...")
                                self.set_connection_type(netbox_vm, commit)
                                connection_type_count += 1
                    
                    except Exception as vm_sync_error:
                        self.log_failure(f"  Erreur synchronisation VM {vm_name}: {str(vm_sync_error)}")
                        import traceback
                        self.log_debug(f"  Traceback: {traceback.format_exc()}")
                        # La transaction est rollback automatiquement, on continue avec la VM suivante
                        continue
                
                else:
                    # Mode DRY-RUN
                    vm_count += 1
                    self.log_info(f"[DRY-RUN] VM {vm_name} serait traitée")
                    self.log_info(f"[DRY-RUN] Taille disque détectée: {total_disk_gb}GB")
                    
                    if platform:
                        self.log_info(f"[DRY-RUN] Plateforme: {platform.name if platform else 'Non disponible'}")
                        platform_count += 1
                    
                    if data.get('sync_virtual_disks', True) and disk_details:
                        self.sync_vm_virtual_disks(None, disk_details, commit)
                        virtual_disk_count += 1
                    
                    if data.get('sync_interfaces', True):
                        vm_status = self.proxmox_get(f'{base_url}/nodes/{node_name}/qemu/{vm_id}/status/current', headers)
                        self.sync_vm_interfaces(None, vm_config, base_url, node_name, vm_id, headers, vm_status, commit)
                        interface_count += 1
                    
                    if data.get('sync_connection_type', True):
                        self.log_info(f"[DRY-RUN] Type de connexion serait analysé")
                        connection_type_count += 1
                    
                    # Vérifier si la VM existe déjà dans NetBox
                    existing_vm = VirtualMachine.objects.filter(name=vm_name, cluster=cluster).first()
                    if existing_vm:
                        vm_updated += 1
                        self.log_info(f"[DRY-RUN] 🔄 VM existante qui serait mise à jour")
                    else:
                        vm_created += 1
                        self.log_info(f"[DRY-RUN] ✨ Nouvelle VM qui serait créée")

            except Exception as e:
                error_vm_name = vm_name if vm_name else vm.get('name', f"VM-{vm.get('vmid', 'unknown')}")
                self.log_failure(f"Erreur lors du traitement de la VM {error_vm_name}: {str(e)}")
                import traceback
                self.log_debug(f"Traceback complet: {traceback.format_exc()}")
                
                # Continuer avec la VM suivante malgré l'erreur
                continue

        # NETTOYAGE: Supprimer les VMs obsolètes
        vm_deleted = 0
        if self.cleanup_obsolete:
            self.log_info(f"\n=== Nettoyage des VMs obsolètes ===")
            try:
                with transaction.atomic():
                    vm_deleted = self.cleanup_obsolete_vms(cluster, proxmox_vm_names, commit)
            except Exception as cleanup_error:
                self.log_failure(f"Erreur lors du nettoyage des VMs: {str(cleanup_error)}")
                import traceback
                self.log_debug(f"Traceback: {traceback.format_exc()}")

        # Résumé final détaillé
        self.log_info("\n" + "="*60)
        self.log_info("=== RÉSUMÉ DE LA SYNCHRONISATION ===")
        self.log_info("="*60)
        
        mode = "COMMIT" if commit else "DRY-RUN"
        result_msg = f"""
Mode: {mode}
{'='*60}

📊 Statistiques de synchronisation:

VMs:
  • Total traitées: {vm_count}
  • Créées: {vm_created}
  • Mises à jour: {vm_updated}
  • Supprimées: {vm_deleted}

Composants synchronisés:"""

        if data.get('sync_virtual_disks', True):
            result_msg += f"\n  • Disques virtuels: {virtual_disk_count} VMs"
        
        if data.get('sync_interfaces', True):
            result_msg += f"\n  • Interfaces réseau: {interface_count} VMs"
        
        if data.get('sync_platforms', True):
            result_msg += f"\n  • Plateformes OS: {platform_count} détectées"
        
        if data.get('set_primary_ip', True):
            result_msg += f"\n  • IPs primaires: vérifiées"
        
        if data.get('sync_connection_type', True):
            result_msg += f"\n  • Types de connexion: {connection_type_count} analysés"
        
        if vms_without_agent:
            result_msg += f"\n\n   VMs sans agent QEMU ({len(vms_without_agent)}):"
            result_msg += "\n  " + ", ".join(vms_without_agent[:5])
            if len(vms_without_agent) > 5:
                result_msg += f"\n  ... et {len(vms_without_agent) - 5} autres"
        
        if self.cleanup_obsolete:
            result_msg += f"\n\n  Nettoyage: {'Activé' if commit else 'Mode simulation'}"
        else:
            result_msg += f"\n\n  Nettoyage: Désactivé"
        
        if not commit:
            result_msg += f"\n\n  MODE DRY-RUN: Aucune modification n'a été effectuée"
            result_msg += f"\n    Relancez avec 'Commit changes' pour appliquer les modifications"
        
        self.log_info(result_msg)
        self.log_info("="*60)
        
        return result_msg
