import asyncio
import aiohttp
import math
from typing import Dict, List, Optional, Tuple
from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import Enum
import os
from fastapi import FastAPI, HTTPException, BackgroundTasks
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
import logging

# Configure basic logging
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')

# Riot API Configuration
RIOT_API_KEY_PLACEHOLDER = "RGAPI-PLEASE-REPLACE-ME-WITH-YOUR-REAL-KEY" # Renamed placeholder
RIOT_API_KEY = os.getenv("RIOT_API_KEY", RIOT_API_KEY_PLACEHOLDER) # Use the new placeholder as default
RIOT_API_BASE = "https://{region}.api.riotgames.com"

# Startup logging for API Key status
if RIOT_API_KEY == RIOT_API_KEY_PLACEHOLDER:
    logging.warning(
        f"WARNING: RIOT_API_KEY is using the placeholder value ('{RIOT_API_KEY_PLACEHOLDER}'). "
        "The application will not function correctly with external Riot API calls. "
        "Ensure the RIOT_API_KEY environment variable is set to your actual key in production."
    )
elif not RIOT_API_KEY: # Should not happen if placeholder is default, but good for robustness
    logging.critical(
        "CRITICAL: RIOT_API_KEY environment variable is not set and no default was provided. "
        "The application will not be able to communicate with the Riot API."
    )
else:
    logging.info("Riot API Key is configured (found in environment or using a non-placeholder default).")


class Region(Enum):
    NA1 = "na1"
    EUW1 = "euw1"
    EUN1 = "eun1"
    KR = "kr"
    BR1 = "br1"
    LA1 = "la1"
    LA2 = "la2"
    OC1 = "oc1"
    TR1 = "tr1"
    RU = "ru"
    JP1 = "jp1"
    PH2 = "ph2"
    SG2 = "sg2"
    TH2 = "th2"
    TW2 = "tw2"
    VN2 = "vn2"

class QueueType(Enum):
    RANKED_SOLO = 420
    RANKED_FLEX = 440
    DRAFT_PICK = 400
    BLIND_PICK = 430

@dataclass
class CalculatedMMR:
    summoner_name: str
    tag_line: str
    region: str
    current_mmr: float
    rank: str
    division: int  # 1-4 (I, II, III, IV)
    lp_equivalent: int
    confidence_level: float  # How accurate the calculation is (0-100%)
    games_analyzed: int
    last_updated: datetime
    
    def to_dict(self) -> dict:
        return {
            "summoner_name": self.summoner_name,
            "tag_line": self.tag_line,
            "region": self.region,
            "mmr": round(self.current_mmr, 1),
            "rank": self.rank,
            "division": self.division,
            "division_display": self._get_division_display(),
            "lp_equivalent": self.lp_equivalent,
            "confidence_level": round(self.confidence_level, 1),
            "games_analyzed": self.games_analyzed,
            "last_updated": self.last_updated.isoformat(),
            "rank_display": f"{self.rank} {self._get_division_display()}"
        }
    
    def _get_division_display(self) -> str:
        division_map = {1: "I", 2: "II", 3: "III", 4: "IV"}
        return division_map.get(self.division, "I")

class RiotAPIClient:
    def __init__(self, api_key: str):
        self.api_key = api_key
        self.session = None
        
    async def __aenter__(self):
        self.session = aiohttp.ClientSession(
            headers={"X-Riot-Token": self.api_key},
            timeout=aiohttp.ClientTimeout(total=30)
        )
        return self
        
    async def __aexit__(self, exc_type, exc_val, exc_tb):
        if self.session:
            await self.session.close()

    async def _handle_riot_api_error(self, response: aiohttp.ClientResponse, url: str, context: str):
        """Helper to log and raise HTTPException for Riot API errors."""
        error_message_for_log = f"Riot API Error ({context}): URL: {url}, Status: {response.status}"
        try:
            riot_error_content = await response.json()
            error_message_for_log += f", Response: {riot_error_content}"
        except Exception:
            riot_error_text = await response.text()
            error_message_for_log += f", Response: {riot_error_text}"
        
        logging.error(error_message_for_log)

        user_facing_detail = f"Error during {context}."
        if response.status == 400:
            user_facing_detail = f"Bad request to Riot API during {context}."
        elif response.status == 401:
            user_facing_detail = "Riot API key is invalid or missing. Check server configuration."
        elif response.status == 403:
            user_facing_detail = "Riot API key does not have permissions for this request or is expired."
        elif response.status == 404:
            user_facing_detail = f"Resource not found via Riot API during {context} (e.g., summoner, match)."
        elif response.status == 429:
            user_facing_detail = "Riot API rate limit exceeded. Please try again later."
        elif response.status >= 500 and response.status <= 504:
            user_facing_detail = f"Riot API server error ({response.status}) during {context}. Please try again later."
        else:
            user_facing_detail = f"Unexpected Riot API error ({response.status}) during {context}."
            
        raise HTTPException(status_code=response.status, detail=user_facing_detail)
    
    async def get_account_by_riot_id(self, game_name: str, tag_line: str, region: str) -> dict:
        """Get account info by Riot ID (name#tag)"""
        # Use regional routing for account-v1
        regional_map = {
            "na1": "americas", "br1": "americas", "la1": "americas", "la2": "americas",
            "euw1": "europe", "eun1": "europe", "tr1": "europe", "ru": "europe",
            "kr": "asia", "jp1": "asia", "oc1": "sea", "ph2": "sea", "sg2": "sea", 
            "th2": "sea", "tw2": "sea", "vn2": "sea"
        }
        
        regional_endpoint = regional_map.get(region.lower(), "americas")
        url = f"https://{regional_endpoint}.api.riotgames.com/riot/account/v1/accounts/by-riot-id/{game_name}/{tag_line}"
        
        async with self.session.get(url) as response:
            if response.status != 200:
                await self._handle_riot_api_error(response, url, "fetching account by Riot ID")
            return await response.json()
    
    async def get_summoner_by_puuid(self, puuid: str, region: str) -> dict:
        """Get summoner info by PUUID"""
        url = f"https://{region}.api.riotgames.com/lol/summoner/v4/summoners/by-puuid/{puuid}"
        
        async with self.session.get(url) as response:
            if response.status != 200:
                await self._handle_riot_api_error(response, url, "fetching summoner by PUUID")
            return await response.json()
    
    async def get_ranked_stats(self, summoner_id: str, region: str) -> dict:
        """Get ranked statistics"""
        url = f"https://{region}.api.riotgames.com/lol/league/v4/entries/by-summoner/{summoner_id}"
        
        async with self.session.get(url) as response:
            if response.status != 200:
                await self._handle_riot_api_error(response, url, "fetching ranked stats")
            return await response.json()
    
    async def get_match_history(self, puuid: str, region: str, queue_type: int, count: int = 20) -> List[str]:
        """Get recent match IDs"""
        regional_map = {
            "na1": "americas", "br1": "americas", "la1": "americas", "la2": "americas",
            "euw1": "europe", "eun1": "europe", "tr1": "europe", "ru": "europe",
            "kr": "asia", "jp1": "asia", "oc1": "sea", "ph2": "sea", "sg2": "sea", 
            "th2": "sea", "tw2": "sea", "vn2": "sea"
        }
        
        regional_endpoint = regional_map.get(region.lower(), "americas")
        url = f"https://{regional_endpoint}.api.riotgames.com/lol/match/v5/matches/by-puuid/{puuid}/ids"
        
        params = {
            "queue": queue_type,
            "type": "ranked",
            "start": 0,
            "count": count
        }
        
        async with self.session.get(url, params=params) as response:
            if response.status != 200:
                await self._handle_riot_api_error(response, url, f"fetching match history (queue: {queue_type})")
            return await response.json()
    
    async def get_match_details(self, match_id: str, region: str) -> dict:
        """Get detailed match information"""
        regional_map = {
            "na1": "americas", "br1": "americas", "la1": "americas", "la2": "americas",
            "euw1": "europe", "eun1": "europe", "tr1": "europe", "ru": "europe",
            "kr": "asia", "jp1": "asia", "oc1": "sea", "ph2": "sea", "sg2": "sea", 
            "th2": "sea", "tw2": "sea", "vn2": "sea"
        }
        
        regional_endpoint = regional_map.get(region.lower(), "americas")
        url = f"https://{regional_endpoint}.api.riotgames.com/lol/match/v5/matches/{match_id}"
        
        async with self.session.get(url) as response:
            if response.status != 200:
                await self._handle_riot_api_error(response, url, f"fetching match details for match ID {match_id}")
            return await response.json()

class MMRCalculator:
    def __init__(self):
        # Base MMR values for each rank
        self.RANK_BASE_MMR = {
            "IRON": {"IV": 400, "III": 500, "II": 600, "I": 700},
            "BRONZE": {"IV": 800, "III": 900, "II": 1000, "I": 1100},
            "SILVER": {"IV": 1200, "III": 1300, "II": 1400, "I": 1500},
            "GOLD": {"IV": 1600, "III": 1700, "II": 1800, "I": 1900},
            "PLATINUM": {"IV": 2000, "III": 2100, "II": 2200, "I": 2300},
            "EMERALD": {"IV": 2400, "III": 2500, "II": 2600, "I": 2700},
            "DIAMOND": {"IV": 2800, "III": 2900, "II": 3000, "I": 3100},
            "MASTER": {"I": 3200},
            "GRANDMASTER": {"I": 3400},
            "CHALLENGER": {"I": 3600}
        }
        
        self.LP_TO_MMR_RATIO = 0.8  # 1 LP ≈ 0.8 MMR roughly
    
    def calculate_base_mmr_from_rank(self, tier: str, rank: str, lp: int) -> float:
        """Calculate base MMR from current rank and LP"""
        tier = tier.upper()
        rank = rank.upper()

        if tier not in self.RANK_BASE_MMR:
            logging.warning(f"Tier '{tier}' not found in RANK_BASE_MMR. Defaulting to unranked MMR (1200).")
            return 1200.0

        # For Master, Grandmaster, Challenger, the division is always "I"
        if tier in ["MASTER", "GRANDMASTER", "CHALLENGER"]:
            if rank != "I":
                logging.info(f"For tier '{tier}', rank is expected to be 'I'. Received '{rank}', correcting to 'I'.")
            rank = "I"
        # Check if the (possibly corrected) rank is valid for the tier's defined divisions
        elif rank not in self.RANK_BASE_MMR[tier]:
            # This handles cases where rank might be invalid for Iron-Diamond
            logging.warning(f"Invalid rank '{rank}' for tier '{tier}'. Valid divisions are {list(self.RANK_BASE_MMR[tier].keys())}. Defaulting to 'IV'.")
            rank = "IV" # Default to IV for Iron-Diamond if rank is unexpected
            # Ensure 'IV' is a valid key if we default to it (it is for Iron-Diamond as defined)
            if rank not in self.RANK_BASE_MMR[tier]:
                 logging.error(f"Critical: Default rank 'IV' is not valid for tier '{tier}' even after attempting to default. Tier divisions: {list(self.RANK_BASE_MMR[tier].keys())}. Returning default MMR (1200).")
                 return 1200.0

        # Final check before accessing, to prevent KeyError if logic above missed a case
        if rank not in self.RANK_BASE_MMR[tier]:
            logging.error(f"FATAL: Rank '{rank}' is still not a valid division for tier '{tier}' before accessing RANK_BASE_MMR. Expected one of {list(self.RANK_BASE_MMR[tier].keys())}. Returning default MMR (1200).")
            return 1200.0

        base_mmr = self.RANK_BASE_MMR[tier][rank]
        lp_bonus = lp * self.LP_TO_MMR_RATIO
        return base_mmr + lp_bonus
    
    def analyze_match_performance(self, matches_data: List[dict], target_puuid: str) -> Tuple[float, int, float]:
        """
        Analyze match performance to estimate MMR
        Returns: (estimated_mmr_adjustment, games_analyzed, confidence)
        """
        if not matches_data:
            return 0.0, 0, 0.0
        
        total_mmr_adjustment = 0.0
        games_analyzed = 0
        performance_factors = []
        
        for match_data in matches_data:
            if not match_data or 'info' not in match_data:
                continue
                
            participants = match_data['info']['participants']
            target_participant = None
            
            # Find our target player
            for participant in participants:
                if participant['puuid'] == target_puuid:
                    target_participant = participant
                    break
            
            if not target_participant:
                continue
            
            games_analyzed += 1
            
            # Calculate performance metrics
            kda = self._calculate_kda_score(target_participant)
            vision_score = target_participant.get('visionScore', 0)
            cs_per_min = target_participant.get('totalMinionsKilled', 0) / max(1, match_data['info']['gameDuration'] / 60)
            damage_ratio = self._calculate_damage_ratio(target_participant, participants)
            
            # Win/Loss impact
            won = target_participant['win']
            base_adjustment = 20 if won else -20
            
            # Performance modifiers
            performance_multiplier = 1.0
            
            # KDA impact
            if kda > 3.0:
                performance_multiplier += 0.2
            elif kda < 1.0:
                performance_multiplier -= 0.2
            
            # Vision impact (support role consideration)
            if vision_score > 30:
                performance_multiplier += 0.1
            
            # CS impact (non-support roles)
            role = target_participant.get('teamPosition', '')
            if role not in ['UTILITY'] and cs_per_min > 7:
                performance_multiplier += 0.1
            elif role not in ['UTILITY'] and cs_per_min < 4:
                performance_multiplier -= 0.1
            
            # Damage contribution
            if damage_ratio > 0.25:
                performance_multiplier += 0.1
            elif damage_ratio < 0.15:
                performance_multiplier -= 0.1
            
            match_adjustment = base_adjustment * performance_multiplier
            total_mmr_adjustment += match_adjustment
            performance_factors.append(abs(performance_multiplier - 1.0))
        
        # Calculate confidence based on consistency
        confidence = 100.0
        if performance_factors:
            avg_variance = sum(performance_factors) / len(performance_factors)
            confidence = max(50.0, 100.0 - (avg_variance * 100))
        
        return total_mmr_adjustment, games_analyzed, confidence
    
    def _calculate_kda_score(self, participant: dict) -> float:
        """Calculate KDA ratio"""
        kills = participant.get('kills', 0)
        deaths = participant.get('deaths', 0)
        assists = participant.get('assists', 0)
        
        if deaths == 0:
            return (kills + assists) * 2  # Perfect KDA bonus
        
        return (kills + assists) / deaths
    
    def _calculate_damage_ratio(self, participant: dict, all_participants: List[dict]) -> float:
        """Calculate damage share compared to team"""
        player_damage = participant.get('totalDamageDealtToChampions', 0)
        team_id = participant.get('teamId')
        
        team_damage = sum(p.get('totalDamageDealtToChampions', 0) 
                         for p in all_participants if p.get('teamId') == team_id)
        
        if team_damage == 0:
            return 0.0
        
        return player_damage / team_damage
    
    def get_rank_and_division_from_mmr(self, mmr: float) -> Tuple[str, int]:
        """Convert MMR back to rank and division"""
        for tier, divisions in self.RANK_BASE_MMR.items():
            for div, base_mmr in divisions.items():
                # Check if MMR falls within this division's range
                next_tier_mmr = self._get_next_tier_mmr(tier, div)
                if base_mmr <= mmr < next_tier_mmr:
                    div_num = {"IV": 4, "III": 3, "II": 2, "I": 1}.get(div, 4)
                    return tier, div_num
        
        # Handle edge cases
        if mmr < 400:
            return "IRON", 4
        else:
            return "CHALLENGER", 1
    
    def _get_next_tier_mmr(self, current_tier: str, current_div: str) -> float:
        """Get the MMR threshold for the next tier/division"""
        tiers_order = ["IRON", "BRONZE", "SILVER", "GOLD", "PLATINUM", "EMERALD", "DIAMOND", "MASTER", "GRANDMASTER", "CHALLENGER"]
        divs_order = ["IV", "III", "II", "I"]
        
        current_tier_idx = tiers_order.index(current_tier)
        current_div_idx = divs_order.index(current_div)
        
        # Next division in same tier
        if current_div_idx > 0:
            next_div = divs_order[current_div_idx - 1]
            return self.RANK_BASE_MMR[current_tier][next_div]
        
        # Next tier
        if current_tier_idx < len(tiers_order) - 1:
            next_tier = tiers_order[current_tier_idx + 1]
            return min(self.RANK_BASE_MMR[next_tier].values())
        
        # Challenger cap
        return float('inf')

class MMRService:
    def __init__(self, riot_api_key: str):
        self.riot_api_key = riot_api_key
        self.calculator = MMRCalculator()
    
    async def calculate_mmr(self, summoner_name: str, tag_line: str, region: str, queue_type: int) -> CalculatedMMR:
        """
        Main function: Calculate MMR for a summoner
        """
        async with RiotAPIClient(self.riot_api_key) as riot_client:
            try:
                # Step 1: Get account info
                account_data = await riot_client.get_account_by_riot_id(summoner_name, tag_line, region)
                puuid = account_data['puuid']
                
                # Step 2: Get summoner data
                summoner_data = await riot_client.get_summoner_by_puuid(puuid, region)
                summoner_id = summoner_data['id']
                
                # Step 3: Get ranked stats
                ranked_data = await riot_client.get_ranked_stats(summoner_id, region)
                
                # Find the specific queue type data
                queue_data = None
                queue_name_map = {420: "RANKED_SOLO_5x5", 440: "RANKED_FLEX_SR"}
                target_queue = queue_name_map.get(queue_type, "RANKED_SOLO_5x5")
                
                for entry in ranked_data:
                    if entry.get('queueType') == target_queue:
                        queue_data = entry
                        break
                
                if not queue_data:
                    raise HTTPException(status_code=404, detail=f"No ranked data found for queue type {queue_type}")
                
                # Step 4: Calculate base MMR from current rank
                tier = queue_data.get('tier', 'IRON')
                rank = queue_data.get('rank', 'IV')
                lp = queue_data.get('leaguePoints', 0)
                
                base_mmr = self.calculator.calculate_base_mmr_from_rank(tier, rank, lp)
                
                # Step 5: Get match history and analyze performance
                match_ids = await riot_client.get_match_history(puuid, region, queue_type, count=20)
                
                matches_data = []
                for match_id in match_ids[:10]:  # Analyze last 10 games for performance
                    match_details = await riot_client.get_match_details(match_id, region)
                    if match_details:
                        matches_data.append(match_details)
                
                # Step 6: Calculate MMR adjustment based on performance
                mmr_adjustment, games_analyzed, confidence = self.calculator.analyze_match_performance(
                    matches_data, puuid
                )
                
                # Step 7: Final MMR calculation
                estimated_mmr = base_mmr + mmr_adjustment
                
                # Step 8: Convert back to rank/division for display
                display_rank, display_division = self.calculator.get_rank_and_division_from_mmr(estimated_mmr)
                
                # Step 9: Calculate LP equivalent
                lp_equivalent = int((estimated_mmr - base_mmr) / self.calculator.LP_TO_MMR_RATIO) + lp
                
                return CalculatedMMR(
                    summoner_name=summoner_name,
                    tag_line=tag_line,
                    region=region,
                    current_mmr=estimated_mmr,
                    rank=display_rank,
                    division=display_division,
                    lp_equivalent=max(0, lp_equivalent),
                    confidence_level=confidence,
                    games_analyzed=games_analyzed,
                    last_updated=datetime.now()
                )
                
            except HTTPException:
                raise
            except Exception as e:
                raise HTTPException(status_code=500, detail=f"MMR calculation failed: {str(e)}")

# FastAPI Application
app = FastAPI(title="League of Legends MMR Calculator", version="2.0.0")

# Ensure the app is accessible for Vercel
handler = app # Vercel expects the FastAPI instance to be named 'app' or 'handler'

app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:3000", "https://your-nextjs-domain.com"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Initialize service
mmr_service = MMRService(RIOT_API_KEY)

# Pydantic models
class MMRRequest(BaseModel):
    summoner_name: str
    tag_line: str
    region: str
    queue_type: int = 420  # Default to Solo/Duo

class MMRResponse(BaseModel):
    success: bool
    data: Optional[dict] = None
    error: Optional[str] = None

@app.post("/calculate-mmr", response_model=MMRResponse)
async def calculate_player_mmr(request: MMRRequest):
    """
    Calculate MMR for a player
    Input: summoner_name, tag_line, region, queue_type
    Output: Calculated MMR with rank and division
    """
    try:
        result = await mmr_service.calculate_mmr(
            summoner_name=request.summoner_name,
            tag_line=request.tag_line,
            region=request.region,
            queue_type=request.queue_type
        )
        
        return MMRResponse(
            success=True,
            data=result.to_dict()
        )
        
    except HTTPException as e:
        # Log the details of the HTTPException if it wasn't already logged by RiotAPIClient
        # (though _handle_riot_api_error should have logged it)
        logging.warning(f"HTTPException caught in endpoint: {e.status_code} - {e.detail}")
        return MMRResponse(
            success=False,
            error=f"API Error: {e.detail}" # e.detail is now the user-facing message
        )
    except Exception as e:
        logging.error(f"Unexpected error during MMR calculation for {request.summoner_name}#{request.tag_line}: {str(e)}", exc_info=True)
        return MMRResponse(
            success=False,
            error=f"An unexpected server error occurred during calculation."
        )

@app.get("/regions")
async def get_supported_regions():
    """Get list of supported regions"""
    return {
        "regions": [
            {"code": "na1", "name": "North America"},
            {"code": "euw1", "name": "Europe West"},
            {"code": "eun1", "name": "Europe Nordic & East"},
            {"code": "kr", "name": "Korea"},
            {"code": "br1", "name": "Brazil"},
            {"code": "la1", "name": "Latin America North"},
            {"code": "la2", "name": "Latin America South"},
            {"code": "oc1", "name": "Oceania"},
            {"code": "tr1", "name": "Turkey"},
            {"code": "ru", "name": "Russia"},
            {"code": "jp1", "name": "Japan"}
        ]
    }

@app.get("/queue-types")
async def get_supported_queues():
    """Get list of supported queue types"""
    return {
        "queues": [
            {"id": 420, "name": "Ranked Solo/Duo"},
            {"id": 440, "name": "Ranked Flex"},
            {"id": 400, "name": "Draft Pick"},
            {"id": 430, "name": "Blind Pick"}
        ]
    }

@app.get("/health")
async def health_check():
    """Health check endpoint"""
    # Check if the API key is configured and not the placeholder
    api_key_is_real = bool(RIOT_API_KEY and RIOT_API_KEY != RIOT_API_KEY_PLACEHOLDER)
    return {
        "status": "healthy",
        "api_key_configured": api_key_is_real, # Updated logic
        "timestamp": datetime.now().isoformat()
    }

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)