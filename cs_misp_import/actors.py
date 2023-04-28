"""CrowdStrike Adversary (Actor) MISP event import.

 _______                        __ _______ __        __ __
|   _   .----.-----.--.--.--.--|  |   _   |  |_.----|__|  |--.-----.
|.  1___|   _|  _  |  |  |  |  _  |   1___|   _|   _|  |    <|  -__|
|.  |___|__| |_____|________|_____|____   |____|__| |__|__|__|_____|
|:  1   |                         |:  1   |
|::.. . |                         |::.. . |
`-------'                         `-------'

   @@@@@@   @@@@@@@   @@@  @@@  @@@@@@@@  @@@@@@@    @@@@@@    @@@@@@   @@@@@@@   @@@ @@@
  @@@@@@@@  @@@@@@@@  @@@  @@@  @@@@@@@@  @@@@@@@@  @@@@@@@   @@@@@@@@  @@@@@@@@  @@@ @@@
  @@!  @@@  @@!  @@@  @@!  @@@  @@!       @@!  @@@  !@@       @@!  @@@  @@!  @@@  @@! !@@
  !@!  @!@  !@!  @!@  !@!  @!@  !@!       !@!  @!@  !@!       !@!  @!@  !@!  @!@  !@! @!!
  @!@!@!@!  @!@  !@!  @!@  !@!  @!!!:!    @!@!!@!   !!@@!!    @!@!@!@!  @!@!!@!    !@!@!
  !!!@!!!!  !@!  !!!  !@!  !!!  !!!!!:    !!@!@!     !!@!!!   !!!@!!!!  !!@!@!      @!!!
  !!:  !!!  !!:  !!!  :!:  !!:  !!:       !!: :!!        !:!  !!:  !!!  !!: :!!     !!:
  :!:  !:!  :!:  !:!   ::!!:!   :!:       :!:  !:!      !:!   :!:  !:!  :!:  !:!    :!:
  ::   :::   :::: ::    ::::     :: ::::  ::   :::  :::: ::   ::   :::  ::   :::     ::
   :   : :  :: :  :      :      : :: ::    :   : :  :: : :     :   : :   :   : :     :
"""
import datetime
import logging
import os
import time
import concurrent.futures
try:
    from pymisp import MISPObject, MISPEvent, ExpandedPyMISP, MISPGalaxyCluster, MISPGalaxyClusterElement
except ImportError as no_pymisp:
    raise SystemExit(
        "The PyMISP package must be installed to use this program."
        ) from no_pymisp

from .adversary import Adversary
from .helper import (
    ADVERSARIES_BANNER,
    confirm_boolean_param,
    display_banner,
    get_threat_actor_galaxy_id,
    get_actor_galaxy_map,
    add_cluster_elements,
    normalize_locale,
    normalize_sector,
    get_region_galaxy_map,
    normalize_killchain
    )

class ActorsImporter:
    """Tool used to import actors from the Crowdstrike Intel API and push them as events in MISP through the MISP API.

    :param misp_client: client for a MISP instance
    :param intel_api_client: client for the Crowdstrike Intel API
    """

    def __init__(self, misp_client, intel_api_client, crowdstrike_org_uuid, actors_timestamp_filename, settings, import_settings, logger = None):
        """Construct an instance of the ActorsImporter class."""
        self.misp: ExpandedPyMISP = misp_client
        self.intel_api_client = intel_api_client
        self.actors_timestamp_filename = actors_timestamp_filename
        self.crowdstrike_org = self.misp.get_organisation(crowdstrike_org_uuid, True)
        self.settings = settings
        self.unknown = import_settings.get("unknown_mapping", "UNIDENTIFIED")
        self.import_settings = import_settings
        self.log: logging.Logger = logger
        self.regions = get_region_galaxy_map(misp_client)


    def batch_import_actors(self, act, act_det, already):
        def do_update(evt):
            return self.misp.add_event(evt, True)

        actor_name = act.get('name')
        act_detail = Adversary[actor_name.split(" ")[1].upper()].value
        info_str = f"ADV-{act.get('id')} {actor_name} ({act_detail})"
        returned = False
        if actor_name is not None:
            if already.get(info_str) is None:
                event: MISPEvent = self.create_event_from_actor(act, act_det)
                self.log.debug("Created adversary event for %s", act.get('name'))
                if event:
                    #try:
                    for tag in self.settings["CrowdStrike"]["actors_tags"].split(","):
                        event.add_tag(tag)
                    # Create an actor specific tag
                    actor_tag = actor_name.split(" ")[1]
                    event.add_tag(f"CrowdStrike:adversary:branch: {actor_tag}")
                    #event.add_tag(f"CrowdStrike:actor: {actor_tag}")
                    if actor_name is not None:
                        already[actor_name] = True
#                   event = self.misp.add_event(event, True)
                    success = False
                    max_tries = 3
                    for cur_try in range(max_tries):
                        try:
                            event = do_update(event)
                            success = True
                        except Exception as err:
                            timeout = 0.3 * 2 ** cur_try
                            self.log.warning("Could not add or tag event %s. Will retry in %s seconds.\n%s", event.info, timeout, str(err))
                            time.sleep(timeout)
                    if not success:
                        self.log.warning("Unable to add event %s.", event.info)

                    if act.get('last_modified_date'):
                        ts_check = 0
                        if os.path.exists(self.actors_timestamp_filename):
                            with open(self.actors_timestamp_filename, 'r', encoding="utf-8") as ts_file:
                                ts_check = ts_file.read()
                        # This might be a little over the top
                        nowstamp = None
                        try:
                            if int(act.get('last_modified_date')) > int(ts_check):
                                nowstamp = act.get('last_modified_date')
                        except ValueError:
                            nowstamp = int(datetime.datetime.today().timestamp())
                        if nowstamp:
                            with open(self.actors_timestamp_filename, 'w', encoding="utf-8") as ts_file:
                                ts_file.write(str(int(nowstamp)+1))
                    returned = True
                else:
                    self.log.warning("Failed to create a MISP event for actor %s.", act)
            else:
                self.log.debug("Actor %s already exists, skipping", actor_name)

        return returned


    def process_actors(self, actors_days_before, events_already_imported):
        """Pull and process actors.

        :param actors_days_before: in case on an initialisation run, this is the age of the actors pulled in days
        :param events_already_imported: the events already imported in misp, to avoid duplicates
        """
        display_banner(banner=ADVERSARIES_BANNER,
                       logger=self.log,
                       fallback="BEGIN ADVERSARIES IMPORT",
                       hide_cool_banners=self.import_settings["no_banners"]
                       )
        #self.log.info(ADVERSARIES_BANNER)
        start_get_events = int((
            datetime.datetime.today() + datetime.timedelta(days=-int(min(actors_days_before, 7300)))
        ).timestamp())

        # Galaxy Clusters
        self.log.info("Start Threat Actor galaxy cluster alignment")
        actors = self.intel_api_client.get_actors(start_get_events, self.import_settings["type"])
        self.log.info("Got %i adversaries from the Crowdstrike Intel API.", len(actors))
        actor_map = get_actor_galaxy_map(self.misp, self.intel_api_client, self.import_settings["type"])
        act_id_list = [x.get("id") for x in actors if x["name"] not in actor_map]
        if act_id_list:
            act_detail = self.intel_api_client.falcon.get_actor_entities(
                ids=act_id_list,
                fields="__full__"
                )["body"]["resources"]
        else:
            act_detail = []
        # Set any inbound CS cluster elements
        for mapped in actor_map.values():
            cluster = self.misp.get_galaxy_cluster(mapped["uuid"])
            details = {}
            for det in [d for d in act_detail if d.get("id") == mapped["cs_id"]]:
                details = det
                add_cluster_elements(details, details, cluster)
            # act_rec = {}
            # for this_act in actors:
            #     if this_act.get("id") == mapped["cs_id"]:
            #         act_rec = this_act

        threat_actors_galaxy = get_threat_actor_galaxy_id(self.misp)
        # Create Threat Actor Galaxy Clusters for missing CS adversaries
        

        for act in [a for a in actors if a["name"] not in actor_map]:
            details = {}
            for det in act_detail:
                if det.get("id") == act.get("id"):
                    details = det
            cluster = MISPGalaxyCluster()
            cluster["distribution"] = 1
            # cluster.distribution = 1
            cluster["authors"] = ["CrowdStrike"]
            # cluster.authors = ["CrowdStrike"]
            cluster["type"] = "threat-actor"
            cluster["default"] = False
            cluster["source"] = "CrowdStrike"
            cluster["description"] = details["description"]
            cluster["value"] = act["name"].upper()
            cluster.Orgc = self.crowdstrike_org
            add_cluster_elements(act, details, cluster)

            cluster_result = self.misp.add_galaxy_cluster(threat_actors_galaxy, cluster)
            actor_map[act['name'].upper()] = {
                "uuid": cluster_result["GalaxyCluster"]["uuid"],
                "tag_name": cluster_result["GalaxyCluster"]["tag_name"],
                "custom": True,
                "name": cluster_result["GalaxyCluster"]["value"],
                "id": cluster_result["GalaxyCluster"]["id"],
                "deleted": cluster_result["GalaxyCluster"]["deleted"],
                "cs_name": act["name"].upper(),
                "cs_id": act["id"]
            }
        # Restore any soft deleted CrowdStrike adversary threat actor clusters
        for act in [a["id"] for a in actor_map.values() if a["deleted"]]:
            # -ca does a hard delete so this will be skipped.
            self.misp._check_json_response(self.misp._prepare_request("POST", f"galaxy_clusters/restore/{act}"))

        self.import_settings["actor_map"] = actor_map
        self.log.info("Threat Actor galaxy alignment complete.")

        if os.path.isfile(self.actors_timestamp_filename):
            with open(self.actors_timestamp_filename, 'r', encoding="utf-8") as ts_file:
                line = ts_file.readline()
                if line:
                    start_get_events = int(line)
        self.log.info(f"Start importing CrowdStrike Adversaries as events into MISP (past {actors_days_before} days).")
        time_send_request = datetime.datetime.now()

        #actors = self.intel_api_client.get_actors(start_get_events, self.import_settings["type"])
        if len(actors) == 0:
            with open(self.actors_timestamp_filename, 'w', encoding="utf-8") as ts_file:
                ts_file.write(str(int(time_send_request.timestamp())))
        else:
            actor_details = self.intel_api_client.falcon.get_actor_entities(ids=[x.get("id") for x in actors], fields="__full__")["body"]["resources"]
            reported = 0
            with concurrent.futures.ThreadPoolExecutor(self.misp.thread_count, thread_name_prefix="thread") as executor:
                futures = {
                    executor.submit(self.batch_import_actors, ac, actor_details, events_already_imported) for ac in actors
                }
                for fut in concurrent.futures.as_completed(futures):
                    if fut.result():
                        reported += 1
            self.log.info("Completed import of %i CrowdStrike adversaries into MISP.", reported)

        self.log.info("Finished importing CrowdStrike Adversaries as events into MISP.")

    @staticmethod
    def create_internal_reference() -> MISPObject:
            inter = MISPObject("internal-reference")
            inter.add_attribute("type", "Adversary detail", disable_correlation=True)

            return inter

    @staticmethod
    def int_ref_handler(evt, kc_name, kc_detail, ref_list, slg, act_name, int_ref, verbose: bool = False):
        kc_items = ["actions_and_objectives", "command_and_control", "delivery", "exploitation", "installation", "reconnaissance", "weaponization", "objectives", "command and control"]
        misp_object = MISPObject("internal-reference")
        misp_object.add_attribute("type", "Adversary detail", disable_correlation=True)
        misp_object.add_attribute("identifier", kc_name.title(), disable_correlation=True)
        if not isinstance(kc_detail, list):
            kc_detail.replace("\t", "").replace("&nbsp;", "")
            sum_id = misp_object.add_attribute("comment", kc_detail, disable_correlation=True)
        ref_list.append(evt.add_object(misp_object))
        if verbose:
            evt.add_attribute_tag(f"CrowdStrike:adversary:{kc_name.lower().replace(' ', '-')}: {act_name}", sum_id.uuid)
            evt.add_attribute_tag(f"CrowdStrike:adversary:{slg}: {kc_name.upper()}", sum_id.uuid)
        if kc_name.lower() in kc_items:
            predicate = "Action on Objectives"
            if normalize_killchain(kc_name) != "objectives":
                predicate = "Initial Foothold"
            evt.add_attribute_tag(f"unified-kill-chain:{predicate}=\"{normalize_killchain(kc_name)}\"", sum_id.uuid)

        int_ref.add_reference(misp_object.uuid, "Adversary detail")

    def create_event_from_actor(self, actor, act_details) -> MISPEvent():
        """Create a MISP event for a valid Actor."""

        event = MISPEvent()
        event.analysis = 2
        event.orgc = self.crowdstrike_org
        if self.import_settings["publish"]:
            event.published = True
        if actor.get('first_activity_date'):
            event.date = actor.get("first_activity_date")
        elif actor.get('last_activity_date'):
            event.date = actor.get("last_activity_date")
        details = {}
        for det in act_details:
            if det.get("id") == actor.get("id"):
                details = det

        actor_name = actor.get("name", None)
        actor_proper_name = " ".join([n.title() for n in actor.get("name", "").split(" ")])
        slug = details.get("slug", actor_name.lower().replace(" ", "-"))
        actor_branch = actor_name.split(" ")[1].upper()
        actor_region = ""
        verbosity = self.import_settings["verbose_tags"]
        if actor_name:
            for act_reg in [adv for adv in dir(Adversary) if "__" not in adv]:
                if act_reg in actor_branch:
                    actor_region = f" ({Adversary[act_reg].value})"
            event.info = f"ADV-{actor.get('id')} {actor_name}{actor_region}"
            actor_att = {
                "type": "threat-actor",
                "value": actor_proper_name,
            }
            if actor_name.upper() in self.import_settings["actor_map"]:
                event.add_tag(self.import_settings["actor_map"][actor_name.upper()]["tag_name"])
            else:
                event.add_tag(f"CrowdStrike:adversary: {actor_name}")

            if details.get('url'):
                event.add_attribute('link', details.get('url'), disable_correlation=True)

            to_reference: list[MISPObject] = []
            internal = None
            # Adversary description
            if details.get('description'):
                internal = self.create_internal_reference()
                internal.add_attribute("identifier", "Description", disable_correlation=True)
                desc_id = internal.add_attribute('comment', details.get('description'), disable_correlation=True)

            # Adversary type
            act_type = details.get("actor_type", None)
            if act_type:
                if not internal:
                    internal = self.create_internal_reference()

                self.int_ref_handler(event, "Actor Type", act_type.title(), to_reference, slug, actor_name, internal, verbosity)
                event.add_tag(f"CrowdStrike:adversary:type: {act_type.upper()}")

            # Adversary motives
            motives = details.get("motivations", None)
            if motives:
                mlist = [m.get("value") for m in motives]
                motive_list_string = "\n".join(mlist)
                if not internal:
                    internal = self.create_internal_reference()

                self.int_ref_handler(event, "Motivation", motive_list_string, to_reference, slug, actor_name, internal, verbosity)
                for mname in mlist:
                    event.add_tag(f"CrowdStrike:adversary:motivation: {mname.upper()}")

            # Adversary capability
            cap = details.get("capability", None)
            if cap:
                cap_val = cap.get("value")
                if cap_val:
                    if not internal:
                        internal = self.create_internal_reference()

                    self.int_ref_handler(event, "Capability", cap_val, to_reference, slug, actor_name, internal, verbosity)
                    event.add_tag(f"CrowdStrike:adversary:capability: {cap_val.upper()}")
                    # Set adversary event threat level based upon adversary capability
                    if "BELOW" in cap_val.upper() or "LOW" in cap_val.upper():
                        event.threat_level_id = 3
                    elif "ABOVE" in cap_val.upper() or "HIGH" in cap_val.upper():
                        event.threat_level_id = 1
                    else:
                        event.threat_level_id = 2

            # Kill chain elements
            kill_chain_detail = details.get("kill_chain")
            if kill_chain_detail:
                objectives = kill_chain_detail.get("actions_and_objectives", None)
                candc = kill_chain_detail.get("command_and_control", None)
                delivery = kill_chain_detail.get("delivery", None)
                exploitation = kill_chain_detail.get("exploitation", None)
                installation = kill_chain_detail.get("installation", None)
                reconnaissance = kill_chain_detail.get("reconnaissance", None)
                weaponization = kill_chain_detail.get("weaponization", None)

                if not internal:
                    internal = self.create_internal_reference()

                # Kill chain - Objectives
                if objectives:
                    self.int_ref_handler(event, "objectives", objectives, to_reference, slug, actor_name, internal, verbosity)

                # Kill chain - Command and Control
                if candc:
                    self.int_ref_handler(event, "command and control", candc, to_reference, slug, actor_name, internal, verbosity)

                # Kill chain - Delivery
                if delivery:
                    self.int_ref_handler(event, "delivery", delivery, to_reference, slug, actor_name, internal, verbosity)

                # Kill chain - Exploitation
                if exploitation:
                    exploitation_object = MISPObject("internal-reference")
                    if exploitation.replace("\t", "".replace("&nbsp;", "")) not in ["Unknown", "N/A"]:
                        exploitation_object.add_attribute("type", "Adversary detail", disable_correlation=True)
                        exploitation_object.add_attribute("identifier", "Exploitation", disable_correlation=True)
                        exploits = exploitation.replace("\t", "").replace("&nbsp;", "").split("\r\n")
                        ex_id = exploitation_object.add_attribute("comment", exploitation.replace("\t", "").replace("&nbsp;", ""), disable_correlation=True)
                        to_reference.append(event.add_object(exploitation_object))
                        event.add_attribute_tag(f"unified-kill-chain:Initial Foothold=\"exploitation\"", ex_id.uuid)
                        if verbosity:
                            event.add_attribute_tag(f"CrowdStrike:adversary:{slug}: EXPLOITATION", ex_id.uuid)
                            event.add_attribute_tag(f"CrowdStrike:adversary:exploitation: {actor_name}", ex_id.uuid)
                            for exptt in [exp for exp in exploits if exp]:
                                if exptt not in ["Unknown", "N/A"]:
                                    for exploit in [a.strip() for a in exptt.split(",")]:
                                        if len(exploit.split(" ")) <= 4:
                                            event.add_attribute_tag(f"CrowdStrike:adversary:exploitation: {exploit.upper()}", ex_id.uuid)
                    internal.add_reference(exploitation_object.uuid, "Adversary detail")
                # Kill chain - Installation
                if installation:
                    self.int_ref_handler(event, "installation", installation, to_reference, slug, actor_name, internal, verbosity)
                    
                # Kill chain - Reconnaissance
                if reconnaissance:
                    self.int_ref_handler(event, "reconnaissance", reconnaissance, to_reference, slug, actor_name, internal, verbosity)
                # Kill chain - Weaponization
                if weaponization:
                    self.int_ref_handler(event, "weaponization", weaponization, to_reference, slug, actor_name, internal, verbosity)

            for ref in to_reference:
                internal.add_reference(ref.uuid, "Adversary detail")
                for web in to_reference:
                    if web.uuid != ref.uuid:
                        web.add_reference(ref.uuid, "Adversary detail")

            if internal:       
                event.add_object(internal)
                # Add the description tags
                if details.get('description') and verbosity:
                    event.add_attribute_tag(f"CrowdStrike:adversary:description: {actor_name}", desc_id.uuid)
                    event.add_attribute_tag(f"CrowdStrike:adversary:{slug}: DESCRIPTION", desc_id.uuid)

            had_timestamp = False
            timestamp_object = MISPObject('timestamp')
            tsf = None
            tsl = None
            actor_att["first_seen"] = actor.get("first_activity_date", 0)
            if not actor_att["first_seen"]:
                self.log.warning("Adversary %s missing field first_activity_date.", actor_name)
            actor_att["last_seen"] = actor.get("last_activity_date", 0)
            if not actor_att["last_seen"]:
                self.log.warning("Adversary %s missing field last_activity_date.", actor_name)
            if actor_att.get("last_seen", 0) < actor_att.get("first_seen", 0):
                # Seems counter-intuitive
                actor_att["first_seen"] = actor.get("last_activity_date")
                actor_att["last_seen"] = actor.get("first_activity_date")
            if actor_att["first_seen"] == 0:
                actor_att["first_seen"] = actor_att["last_seen"]
            if actor_att["first_seen"]:
                tsf = timestamp_object.add_attribute('first-seen', datetime.datetime.utcfromtimestamp(actor_att["first_seen"]).isoformat())
                had_timestamp = True

            if actor_att["last_seen"]:
                tsl = timestamp_object.add_attribute('last-seen', datetime.datetime.utcfromtimestamp(actor_att["last_seen"]).isoformat())
                had_timestamp = True

            ta = event.add_attribute(**actor_att, disable_correlation=True)
            actor_split = actor_name.split(" ")
            actor_branch = actor_split[1] if len(actor_split) > 1 else actor_split[0]
            event.add_attribute_tag(f"CrowdStrike:adversary:branch: {actor_branch}", ta.uuid)
            if had_timestamp:
                event.add_object(timestamp_object)
                if tsf and verbosity:
                    event.add_attribute_tag(f"CrowdStrike:adversary:first-seen: {actor_name}", tsf.uuid)
                    event.add_attribute_tag(f"CrowdStrike:adversary:{slug}: FIRST SEEN", tsf.uuid)
                if tsl and verbosity:
                    event.add_attribute_tag(f"CrowdStrike:adversary:last-seen: {actor_name}", tsl.uuid)
                    event.add_attribute_tag(f"CrowdStrike:adversary:{slug}: LAST SEEN", tsl.uuid)
            if actor.get('known_as') or actor.get("origins"):
                if actor.get("known_as"):
                    known_as_object = MISPObject('organization')
                    aliased = [a.strip() for a in actor.get("known_as").split(",")]
                    for alias in aliased:
                        kao = known_as_object.add_attribute('alias', alias, disable_correlation=True)
                        # Tag the aliases to the threat-actor attribution
                        if verbosity and kao:
                            kao.add_tag(f"CrowdStrike:adversary:branch: {actor_branch}")
                            kao.add_tag(f"CrowdStrike:adversary:{slug}:alias: {alias.upper()}")
                            event.add_attribute_tag(f"CrowdStrike:adversary:{slug}:alias: {alias.upper()}", ta.uuid)
                    event.add_object(known_as_object)
                for orig in actor.get("origins", []):
                    locale = orig.get("value")
                    if locale:
                        kar = event.add_attribute("country-of-residence", locale, disable_correlation=True)
                        event.add_tag(f"CrowdStrike:adversary:origin: {locale.upper()}")
                        if verbosity:
                            event.add_attribute_tag(f"CrowdStrike:adversary:{slug}:origin: {locale.upper()}", kar.uuid)
                            event.add_attribute_tag(f"CrowdStrike:adversary:origin: {locale.upper()}", kar.uuid)

            victim = None
            # Adversary victim location
            if actor.get("target_countries"):
                region_list = [c.get('value') for c in actor.get('target_countries', [])]
                for region in region_list:
                    #event.add_tag(f"misp-galaxy:target-information=\"{region}\"")
                    if not victim:
                        victim = MISPObject("victim")
                    vic = victim.add_attribute('regions', region, disable_correlation=True)
                    region = normalize_locale(region)
                    if region in self.regions:
                        self.log.debug("Regional match. Tagging %s", self.regions[region])
                        event.add_tag(self.regions[region])
                    else:
                        self.log.debug("Country match. Tagging %s.", region)
                        event.add_tag(f"misp-galaxy:target-information=\"{region}\"")
                    if verbosity:
                        vic.add_tag(f"CrowdStrike:target:location: {region.upper()}")
                        vic.add_tag(f"CrowdStrike:adversary:{slug}:target:location: {region.upper()}")

            # Adversary victim industry
            if actor.get("target_industries"):
                sector_list = [s.get('value') for s in actor.get('target_industries', [])]
                for sector in sector_list:
                    if not victim:
                        victim = MISPObject("victim")
                    vic = victim.add_attribute('sectors', sector, disable_correlation=True)
                    if verbosity:
                        vic.add_tag(f"CrowdStrike:adversary:{slug}:target:sector: {sector.upper()}")
                        vic.add_tag(f"CrowdStrike:target:sector: {sector.upper()}")
                    event.add_tag(f"misp-galaxy:sector=\"{normalize_sector(sector)}\"")
            if victim:
                event.add_object(victim)

            # TYPE Taxonomic tag, all events
            if confirm_boolean_param(self.settings["TAGGING"].get("taxonomic_TYPE", False)):
                event.add_tag('type:CYBINT')
            # INFORMATION-SECURITY-DATA-SOURCE Taxonomic tag, all events
            if confirm_boolean_param(self.settings["TAGGING"].get("taxonomic_INFORMATION-SECURITY-DATA-SOURCE", False)):
                event.add_tag('information-security-data-source:integrability-interface="api"')
                event.add_tag('information-security-data-source:originality="original-source"')
                event.add_tag('information-security-data-source:type-of-source="security-product-vendor-website"')
            if confirm_boolean_param(self.settings["TAGGING"].get("taxonomic_IEP", False)):
                event.add_tag('iep:commercial-use="MUST NOT"')
                event.add_tag('iep:provider-attribution="MUST"')
                event.add_tag('iep:unmodified-resale="MUST NOT"')
            if confirm_boolean_param(self.settings["TAGGING"].get("taxonomic_IEP2", False)):
                if confirm_boolean_param(self.settings["TAGGING"].get("taxonomic_IEP2_VERSION", False)):
                    event.add_tag('iep2-policy:iep_version="2.0"')
                event.add_tag('iep2-policy:attribution="must"')
                event.add_tag('iep2-policy:unmodified_resale="must-not"')
            if confirm_boolean_param(self.settings["TAGGING"].get("taxonomic_TLP", False)):
                event.add_tag("tlp:amber")

        else:
            self.log.warning("Adversary %s missing field name.", actor.get('id'))

        return event