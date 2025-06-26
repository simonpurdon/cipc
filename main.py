# main.py

import re
import uuid
import asyncio
import logging
import traceback
import os

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware

from pydantic import BaseModel
from playwright.async_api import async_playwright, TimeoutError as PlaywrightTimeout


logging.basicConfig(
    format="%(asctime)s %(levelname)s %(message)s",
    level=logging.DEBUG
)

app = FastAPI()

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # Configure this for your frontend domain in production
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

_sessions: dict[str, dict] = {}  # token → {"pw","browser","context","created"}

class ConnectRequest(BaseModel):
    username: str  # SA ID number or CIPC customer code
    password: str


class SearchRequest(BaseModel):
    session_token: str
    query: str  # enterprise name or Directory ID


def _map_status_from_src(src: str) -> str:
    if "verify_tick_sml.png" in src:
        return "IN BUSINESS"
    if "verify_orange_sml.png" in src:
        return "IN DEREGISTRATION PROCESS"
    if "verify_cross_sml.png" in src:
        return "FINAL DEREGISTRATION"
    return "UNKNOWN"

@app.get("/")
async def health_check():
    return {"status": "healthy", "service": "CIPC BizPortal Automation"}

@app.post("/connect")
async def connect(req: ConnectRequest):
    token = str(uuid.uuid4())
    pw      = await async_playwright().start()

    # Determine if we're in production based on environment variable
    is_production = os.getenv("ENVIRONMENT", "development") == "production"
    
    browser = await pw.chromium.launch(
        headless=is_production,  # Headless in production, headed in development
        args=[
            '--no-sandbox',
            '--disable-setuid-sandbox',
            '--disable-dev-shm-usage',
            '--disable-gpu'
        ] if is_production else []
    )
    
    context = await browser.new_context()
    page    = await context.new_page()

    try:
        # 1) Go to bizprofile.aspx (redirects you to login.aspx)
        await page.goto("https://www.bizportal.gov.za/bizprofile.aspx", timeout=30000)

        # 2) Wait for the login panel
        await page.wait_for_selector("#cntMain_pnlLogin", timeout=30000)

        # 3) Fill & submit
        await page.fill("input#cntMain_txtIDNo", req.username)
        await page.fill("input#cntMain_txtPassword", req.password)
        await page.click("input#cntMain_btnLogin")

        # 4) Brief pause for error banner
        await asyncio.sleep(2)
        if err_el := await page.query_selector("#cntMain_lblError"):
            raise ValueError((await err_el.text_content() or "").strip())

        # 5) Wait for redirect to your profile page
        await page.wait_for_url("**/user_profile.aspx", timeout=20000)
        await page.wait_for_selector("#cntMain_gdvCompanyList", timeout=20000)

        # 6) Scrape the enterprises table
        rows = await page.query_selector_all("#cntMain_gdvCompanyList tbody tr")
        enterprises = []
        for row in rows[1:]:  # skip header
            cells = await row.query_selector_all("td")
            no   = (await cells[0].text_content() or "").strip()
            name = (await cells[1].text_content() or "").strip()
            src_status = await (await cells[2].query_selector("img")).get_attribute("src")
            src_ar     = await (await cells[3].query_selector("img")).get_attribute("src")
            enterprises.append({
                "enterprise_number": no,
                "enterprise_name":   name,
                "status":            _map_status_from_src(src_status),
                "ar_status":         _map_status_from_src(src_ar),
            })

    except ValueError as ve:
        # bad credentials or known login error
        await page.close()
        await browser.close()
        await pw.stop()
        raise HTTPException(401, detail=str(ve))

    except Exception as e:
        # unexpected error → diagnostics
        logging.error("Login/navigation error: %s", e)
        traceback.print_exc()

        # dump a screenshot + HTML
        screenshot = f"screenshots/{token}_connect.png"
        htmlfile   = f"screenshots/{token}_connect.html"
        try:
            await page.screenshot(path=screenshot, full_page=True)
            with open(htmlfile, "w", encoding="utf-8") as f:
                f.write(await page.content())
            logging.info("Saved diagnostics to %s and %s", screenshot, htmlfile)
        except Exception as dump_err:
            logging.error("Failed to write diagnostics: %s", dump_err)

        await page.close()
        await browser.close()
        await pw.stop()
        raise HTTPException(500, detail=f"Login/navigation error: {e}")

    # success!
    await page.close()  # close this tab
    # keep pw & browser & context alive for /search
    _sessions[token] = {
        "pw": pw,
        "browser": browser,
        "context": context,
        "created": asyncio.get_event_loop().time()
    }

    # **Important**: always return your JSON at the end
    return {
        "success":       True,
        "session_token": token,
        "enterprises":   enterprises
    }


@app.post("/search")
async def search_company(req: SearchRequest):
    session = _sessions.get(req.session_token)
    if not session:
        raise HTTPException(401, detail="Session expired or invalid")

    context = session["context"]
    page = await context.new_page()
    
    try:
        # 1) Navigate to bizprofile.aspx to get fresh tokens
        await page.goto("https://www.bizportal.gov.za/bizprofile.aspx", timeout=30000)
        await page.wait_for_selector("#cntMain_pnlSearchBox", timeout=10000)
        
        # 2) Select "Enterprise No." from dropdown
        # First, get the current state to detect when AJAX completes
        await page.select_option("#cntMain_drpSearchOptions", "EntNo")
        
        # 3) Wait for AJAX postback to complete
        # The postback updates the form, so we wait for the input to be ready
        await asyncio.sleep(1)  # Brief pause for AJAX to start
        try:
            await page.wait_for_selector("#cntMain_txtSearchCIPC:not([disabled])", timeout=10000)
        except PlaywrightTimeout:
            logging.warning("Timeout waiting for search input to be enabled after dropdown change")
        
        # 4) Fill in the enterprise number
        search_input = await page.query_selector("#cntMain_txtSearchCIPC")
        if search_input:
            await search_input.fill("")  # Clear first
            await search_input.fill(req.query)
        else:
            raise ValueError("Could not find search input field")
        
        # 5) Click the search button
        search_button = await page.query_selector("#cntMain_btnSearch")
        if search_button:
            await search_button.click()
        else:
            raise ValueError("Could not find search button")
        
        # Add a brief wait for the search to process
        await asyncio.sleep(2)
        
        # 6) Wait for results panel to appear
        try:
            # First check if results are already loaded
            results_panel = await page.query_selector("#cntMain_pnlResults")
            if not results_panel:
                # If not visible, wait for it
                await page.wait_for_selector("#cntMain_pnlResults", timeout=15000)
            
            # Also verify we have the actual data by checking for a key element
            await page.wait_for_selector("#cntMain_lblEntNo", timeout=5000)
            
        except PlaywrightTimeout:
            # Check if we got an error message or truly no results
            error_msg = await page.query_selector(".error-message")  # Adjust selector as needed
            if error_msg:
                error_text = (await error_msg.text_content()).strip()
                await page.close()
                return {
                    "success": False,
                    "error": error_text,
                    "message": f"Search failed: {error_text}"
                }
            
            # No results found
            await page.close()
            return {
                "success": True,
                "data": [],
                "message": f"No results found for enterprise number: {req.query}"
            }
        
        # 7) Extract enterprise details from the results panel
        
        # Company Details Tab (already visible)
        company_details = {}
        
        # Extract all company details
        ent_no = await page.query_selector("#cntMain_lblEntNo")
        company_details["enterpriseNumber"] = (await ent_no.text_content()).strip() if ent_no else None
        
        ent_name = await page.query_selector("#cntMain_lblEntName")
        company_details["enterpriseName"] = (await ent_name.text_content()).strip() if ent_name else None
        
        ent_type = await page.query_selector("#cntMain_lblEntType")
        company_details["enterpriseType"] = (await ent_type.text_content()).strip() if ent_type else None
        
        ent_status = await page.query_selector("#cntMain_lblEntStatus")
        company_details["enterpriseStatus"] = (await ent_status.text_content()).strip() if ent_status else None
        
        compliance = await page.query_selector("#cntMain_lblNonComply")
        compliance_text = (await compliance.text_content()).strip() if compliance else None
        company_details["complianceNotice"] = None if compliance_text == "NONE" else compliance_text
        
        # Format registration date
        reg_date_elem = await page.query_selector("#cntMain_lblRegDate")
        reg_date_raw = (await reg_date_elem.text_content()).strip() if reg_date_elem else None
        if reg_date_raw and "/" in reg_date_raw:
            parts = reg_date_raw.split("/")
            if len(parts) == 3:
                company_details["registrationDate"] = f"{parts[0]}-{parts[1].zfill(2)}-{parts[2].zfill(2)}"
            else:
                company_details["registrationDate"] = reg_date_raw
        else:
            company_details["registrationDate"] = reg_date_raw
        
        # Format addresses
        phys_addr_elem = await page.query_selector("#cntMain_lblPhysAddress")
        phys_addr_raw = (await phys_addr_elem.text_content()).strip() if phys_addr_elem else None
        company_details["physicalAddress"] = phys_addr_raw.replace("\n", ", ") if phys_addr_raw else None
        
        postal_addr_elem = await page.query_selector("#cntMain_lblPostalAddress")
        postal_addr_raw = (await postal_addr_elem.text_content()).strip() if postal_addr_elem else None
        company_details["postalAddress"] = postal_addr_raw.replace("\n", ", ") if postal_addr_raw else None
        
        # Directors Tab
        directors = []
        try:
            directors_tab = await page.query_selector("label[for='tab-2r']")
            if directors_tab:
                await directors_tab.click()
                await asyncio.sleep(0.5)
                
                director_rows = await page.query_selector_all("#cntMain_gdvDirectorDetails tbody tr")
                for row in director_rows[1:]:  # Skip header
                    cells = await row.query_selector_all("td")
                    if len(cells) >= 5:
                        directors.append({
                            "idNumber": (await cells[0].text_content()).strip(),
                            "names": (await cells[1].text_content()).strip(),
                            "surname": (await cells[2].text_content()).strip(),
                            "type": (await cells[3].text_content()).strip(),
                            "status": (await cells[4].text_content()).strip()
                        })
        except Exception as e:
            logging.warning(f"Could not extract director details: {e}")
        
        # Annual Returns Tab
        annual_returns = {"filedAnnualReturns": [], "outstandingAnnualReturns": []}
        try:
            ar_tab = await page.query_selector("label[for='tab-3r']")
            if ar_tab:
                await ar_tab.click()
                await asyncio.sleep(0.5)
                
                # Filed Annual Returns
                filed_rows = await page.query_selector_all("#cntMain_gdvARPaid tbody tr")
                for row in filed_rows[1:]:  # Skip header
                    cells = await row.query_selector_all("td")
                    if len(cells) >= 3 and "No annual returns" not in (await cells[0].text_content()):
                        year_text = (await cells[0].text_content()).strip()
                        annual_returns["filedAnnualReturns"].append({
                            "year": int(year_text) if year_text.isdigit() else year_text,
                            "amountPaid": (await cells[1].text_content()).strip(),
                            "dateFiled": (await cells[2].text_content()).strip()
                        })
                
                # Outstanding Annual Returns
                outstanding_rows = await page.query_selector_all("#cntMain_gdvAROutstanding tbody tr")
                for row in outstanding_rows[1:]:  # Skip header
                    cells = await row.query_selector_all("td")
                    if len(cells) >= 3:
                        year_text = (await cells[0].text_content()).strip()
                        annual_returns["outstandingAnnualReturns"].append({
                            "year": int(year_text) if year_text.isdigit() else year_text,
                            "month": (await cells[1].text_content()).strip(),
                            "nonComplianceDate": (await cells[2].text_content()).strip()
                        })
        except Exception as e:
            logging.warning(f"Could not extract annual returns: {e}")
        
        # Enterprise History Tab
        history = []
        try:
            history_tab = await page.query_selector("label[for='tab-4r']")
            if history_tab:
                await history_tab.click()
                await asyncio.sleep(0.5)
                
                history_rows = await page.query_selector_all("#cntMain_gdvEntHist tbody tr")
                for row in history_rows[1:]:  # Skip header
                    cells = await row.query_selector_all("td")
                    if len(cells) >= 2:
                        date_raw = (await cells[0].text_content()).strip()
                        # Convert date format from YYYY/MM/DD to YYYY-MM-DD
                        if "/" in date_raw:
                            parts = date_raw.split("/")
                            if len(parts) == 3:
                                date_formatted = f"{parts[0]}-{parts[1].zfill(2)}-{parts[2].zfill(2)}"
                            else:
                                date_formatted = date_raw
                        else:
                            date_formatted = date_raw
                            
                        history.append({
                            "date": date_formatted,
                            "details": (await cells[1].text_content()).strip()
                        })
        except Exception as e:
            logging.warning(f"Could not extract history: {e}")
        
        # Information Regulator Compliance Tab
        compliance_information = {
            "regulatorRegistrationNumber": None,
            "organisationType": None,
            "privateOrganisationType": None,
            "informationOfficerDetails": {
                "surname": None,
                "names": None,
                "designation": None,
                "appointmentDate": None
            },
            "deputyInformationOfficerDetails": {
                "surname": None,
                "names": None,
                "type": None,
                "designation": None,
                "appointmentDate": None
            },
            "paiaAnnualReporting": {
                "submissionHistory": [],
                "latestSubmissionNote": None
            }
        }
        
        try:
            # Check if there's an Information Regulator tab
            ir_tab = await page.query_selector("label[for='tab-10r']")
            if ir_tab:
                await ir_tab.click()
                await asyncio.sleep(0.5)
                
                # Check if the entity is registered
                not_registered = await page.query_selector("#cntMain_pnlInfoRegNotRegistered")
                if not not_registered or (await not_registered.get_attribute("style") and "display: none" in await not_registered.get_attribute("style")):
                    # Entity is registered, extract the data
                    # Note: The selectors below are hypothetical as they weren't in your HTML
                    # You'll need to update these based on the actual registered entity HTML structure
                    
                    reg_num_element = await page.query_selector("#cntMain_lblIRRegNumber")
                    if reg_num_element:
                        compliance_information["regulatorRegistrationNumber"] = (await reg_num_element.text_content()).strip()
                    
                    org_type_element = await page.query_selector("#cntMain_lblOrgType")
                    if org_type_element:
                        compliance_information["organisationType"] = (await org_type_element.text_content()).strip()
                    
                    # Extract other compliance fields if they exist...
                    # This is a placeholder structure - you'll need the actual selectors
                    
        except Exception as e:
            logging.warning(f"Could not extract compliance information: {e}")
        
        # Other Details Tab
        other_details = {}
        try:
            other_tab = await page.query_selector("label[for='tab-7r']")
            if other_tab:
                await other_tab.click()
                await asyncio.sleep(0.5)
                
                tax_element = await page.query_selector("#cntMain_lblTax")
                other_details["sarsTaxNumber"] = (await tax_element.text_content()).strip() if tax_element else None
                
                uif_element = await page.query_selector("#cntMain_lblUIF")
                uif_text = (await uif_element.text_content()).strip() if uif_element else None
                other_details["uifRegNumber"] = None if uif_text == "NOT AVAILABLE" else uif_text
                
                cf_element = await page.query_selector("#cntMain_lblCF")
                cf_text = (await cf_element.text_content()).strip() if cf_element else None
                other_details["compensationFundRegNum"] = None if cf_text == "NOT AVAILABLE" else cf_text
        except Exception as e:
            logging.warning(f"Could not extract other details: {e}")
        
        # Close the page (keep session alive)
        await page.close()
        
        # 11) Return the comprehensive data
        return {
            "success": True,
            "data": {
                "company_details": company_details,
                "directors": directors,
                "annual_returns": annual_returns,
                "history": history,
                "compliance_information": compliance_information,
                "other_details": other_details
            }
        }
        
    except ValueError as ve:
        # Known error (like no view link)
        await page.close()
        raise HTTPException(400, detail=str(ve))
        
    except PlaywrightTimeout as pte:
        # Timeout error
        logging.error("Timeout during search: %s", pte)
        
        # Save diagnostics
        screenshot = f"screenshots/{req.session_token}_search_timeout.png"
        htmlfile = f"screenshots/{req.session_token}_search_timeout.html"
        try:
            await page.screenshot(path=screenshot, full_page=True)
            with open(htmlfile, "w", encoding="utf-8") as f:
                f.write(await page.content())
            logging.info("Saved timeout diagnostics to %s and %s", screenshot, htmlfile)
        except Exception:
            pass
        
        await page.close()
        raise HTTPException(500, detail="Search operation timed out")
        
    except Exception as e:
        # Unexpected error
        logging.error("Search error: %s", e)
        traceback.print_exc()
        
        # Save diagnostics
        screenshot = f"screenshots/{req.session_token}_search_error.png"
        htmlfile = f"screenshots/{req.session_token}_search_error.html"
        try:
            await page.screenshot(path=screenshot, full_page=True)
            with open(htmlfile, "w", encoding="utf-8") as f:
                f.write(await page.content())
            logging.info("Saved error diagnostics to %s and %s", screenshot, htmlfile)
        except Exception as dump_err:
            logging.error("Failed to write diagnostics: %s", dump_err)
        
        await page.close()
        raise HTTPException(500, detail=f"Search error: {str(e)}")


# Optional: Add a disconnect endpoint to clean up sessions
@app.post("/disconnect")
async def disconnect(session_token: str):
    session = _sessions.get(session_token)
    if not session:
        return {"success": False, "message": "Session not found"}
    
    try:
        await session["browser"].close()
        await session["pw"].stop()
        del _sessions[session_token]
        return {"success": True, "message": "Session closed"}
    except Exception as e:
        logging.error("Error closing session: %s", e)
        return {"success": False, "message": f"Error closing session: {str(e)}"}